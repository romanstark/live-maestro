# Architecture: Live Maestro

Live Maestro bridges AI assistants to Ableton Live's Object Model (LOM). Instead of writing dedicated handlers inside Live for every individual property, the architecture describes the LOM surface as declarative data (a YAML catalog) and executes path resolution, validation, and verification through a shared engine.

Key architectural concepts:
- **Generic LOM Bridge:** The Remote Script inside Live focuses on generic object navigation and execution. Handler families cover property access, method calls, and specialized objects across Live.
- **Data-Driven Catalog:** YAML definitions paired with a path builder and executor. Adding support for a new Live property requires only a new catalog entry, not new code inside Live.
- **High-Level MCP Tools with Escape Hatches:** Common production tasks use intent-based tools (`get_session`, `write_clip_notes`, `set_mix`), while low-level access remains available via generic LOM tools (`lom_get`, `lom_set`, `lom_call`).
- **Read-Back Verification:** Parameter changes are verified by reading values back from Live to confirm whether values were applied, quantized, or clamped.

See also: [protocol.md](protocol.md) for the wire specification, [catalog.md](catalog.md) for the catalog schema, and [limits.md](limits.md) for known API boundaries.

---

## The restart tax: Why "as data" matters here

Live loads Remote Scripts only at startup and reuses cached bytecode in `__pycache__` if present. Modifying code inside Live requires restarting Live completely. In contrast, updating catalog files on the Python server side requires no restart of the DAW.

```
Per-command bridge: N handlers inside Live -> every fix requires restarting Live
Data-driven bridge: Generic handler set   -> a catalog change needs a server restart
```

As a general rule, any capability that can be represented as a path within the object model should be defined in the catalog rather than as a bespoke Remote Script handler. Custom handlers inside Live are reserved for operations that generic path navigation cannot handle, such as MIDI note arrays, automation breakpoint interpolation, browser traversal, and event listeners.

---

## Three channels into Live

Maestro reaches Live through three complementary channels, expanding on the original two channels into Live:

```
                    +--------------------------------------------------------+
   MCP client  ---> |                      live-maestro                      |
                    |                                                        |
                    |  Channel A: LOM     Channel B: .als    Channel C: M4L  |
                    +-------+--------------------+-----------------+---------+
                            |                    |                 |
                   TCP 127.0.0.1:9878       file on disk    UDP 127.0.0.1:9882
                            |                    |                 |
                            v                    v                 v
                   live-remote-script      Project.als         LiveMaestro
                   (in Live's process)     (gzip XML)            Analyzer
                            |                    |            (M4L on Master)
                            v                    v                 |
                      Ableton Live          saved set on           v
                      (live memory)             disk          audio stream
```

| Feature | Channel A (LOM) | Channel B (`.als` file) | Channel C (LiveMaestro Analyzer) |
|---|---|---|---|
| **Availability** | While Live is running | While the target set is closed in Live (an edit to a set Live has open is lost when Live next saves) | While Live is running, with the device loaded on the track to measure |
| **State** | In-memory session state | Saved project on disk | The signal passing the device now |
| **Primary Use** | Real-time session control and automation | Reading/writing project files offline (set not loaded in Live) | Loudness, peak, stereo phase and band balance of the audio itself |

Channel B complements the live bridge by handling data that the LOM does not expose in the open set, such as track automation in arrangements or reading unopened projects. Channel C answers what neither of the others can: how the audio measures. A parameter value is not a level, and a level is not a loudness.

File workflows also operate independently of the three channels. `match_sound`
compares library previews and `transcribe_stem` runs a separate transcriber.
`analyze_drums` uses its own optional NumPy/SciPy worker, with persisted progress,
cancellation, JSON/MIDI and one-shot outputs. They work while Live is closed.
`write_drum_clip` then places reviewed events through Channel A. See
[drum workflows](drum-workflows.md) for the shared source timeline and tempo import.

### Channel C: Audio measurement

Channels A and B carry metadata. The Remote Script interpreter has no access to audio buffers at all, so neither channel can report whether a mix clips, how loud it is, or whether its low end survives being summed to mono. `analyze_audio` asks a device that can.

`devices/LiveMaestro_Analyzer.amxd` is a Max for Live audio effect. It passes audio through unchanged, measures what passes, and answers one request with one reply. It is optional: only `analyze_audio` needs it, and without it reports `device_not_found` and the steps to load it.

**Wire format.** Open Sound Control over loopback UDP, because that is what Max's own objects speak: `udpsend`'s reference page states that Max messages are serialized as OSC-compatible UDP packets, and `udpreceive` decodes OSC. The device listens on `127.0.0.1:9882` for `/maestro/measure` and answers `/maestro/report` to the port the request names, which is the ephemeral port the client bound. `src/live_maestro/analyzer/protocol.py` holds the port, the addresses, the reply field order, the band edges and the schema number, and nothing else may hold a copy of any of them.

**The device is generated.** `scripts/build_analyzer_device.py` reads that contract and writes both `LiveMaestro_Analyzer.maxpat`, which `git diff` can show, and `LiveMaestro_Analyzer.amxd`, which Live loads. `--check` fails when either file no longer matches the generator, and a test runs it, so the device in the repository is the device the generator describes and cannot disagree with the client about the contract.

**What it measures.** `loudness~` supplies EBU R128 momentary, short term and integrated loudness, loudness range, sample peak and true peak. Two `svf~` sections per band edge split the mono sum into seven bands, and `average~` in RMS mode reads each one. Three more averages give the channel levels and the mean of their product, which is what a correlation needs, and four more do the same below 120 Hz. One `snapshot~` per average reads them all on the same bang, so every figure in a reply is taken at one instant.

**Which track answered.** A device knows the name and the Live Object Model id of the track it sits on, and not its index: `live.path` reports an id and no canonical path ([docs/limits.md](limits.md)). A caller's index or `master` is resolved to a name over Channel A, in `server.analyze_audio`, and the match is made on the name. The id comes back too, so a set with two tracks of the same name is reported as ambiguous rather than measured silently.

**Where the work is done.** The device reports raw amplitudes and the six figures `loudness~` produces. Every dB value, correlation, crest factor, centroid and verdict is computed in `analyzer/metrics.py` and `analyzer/report.py`, which touch no socket. That is what lets the thresholds and the wording of every recommendation be tested without Live running.

**It measures itself.** The device generates a 1 kHz sine of amplitude 0.5, whose peak is therefore -6.0206 dBFS by construction, and feeds it to a second `loudness~` wired nowhere near the output. Every reply carries what that meter reports for a peak whose value is already known, which is how the sample peak correction is a measurement rather than a constant. [docs/limits.md](limits.md) records what each peak outlet does with it.

**Boundaries.** The averaged figures cover at most 96000 samples of history, which is one second at 96 kHz and 2.18 seconds at 44.1 kHz. The reply reports the window it actually covered. Loudness keeps its own EBU windows and is not affected by that ceiling. The band filters are two state variable sections per edge, not a calibrated filterbank, so band levels are relative indicators. The reported centre frequency is the energy weighted centre of those seven band levels and not the spectral centroid of an FFT, which would need `pfft~` and therefore a subpatcher file, which would stop the device from being one file.

Every measurement is of the signal at the point in the chain where the device sits. It says nothing about the room.

---

## Layers

The codebase is strictly separated into distinct layers:

```
catalog/*.yaml           Declarative catalog defining paths, types, access modes, and validation.
   |
spec.py                  PathSpec, ParamSpec, ArgSpec, path resolution, and value validation.
   |
registry.py              Loads catalog files, aggregates specs, and provides lookup indices.
   |
executor.py              Handles validation, guards, dispatch, error mapping, and verification.
   |
client.py                TCP client handling JSON encoding, timeouts, and connection locking.
   |
live-remote-script/      Python Remote Script running inside Live's process.
```

Supporting modules:
- `analyzer/`: Channel C. `protocol.py` is the wire contract and its OSC codec, `client.py` the socket, `metrics.py` the arithmetic as pure functions, `report.py` the choosing and wording, `models.py` what the caller receives. Only `client.py` needs Live to be running.
- `lom/paths.py`: Client-side parsing and validation of LOM path grammar.
- `lom/introspect.py`: Runtime inspection of dynamic device parameters and session snapshots.
- `music/`: Pure Python musical theory, chord voicings, note transformations, groove humanization, `launch.py` for when a fired clip actually starts, and `transcription.py` for cleaning up what a transcriber heard before any of it is written into Live.
- `als/`: Parsers and writers for `.als` project files.
- `audio/`: Nothing here touches Live. `decode.py` turns any file the system decoder reads into mono samples, `descriptor.py` reduces a piece of audio to a fixed vector that can be compared with another, and `sample_header.py` reads container metadata with the standard library alone, which is how an encrypted factory sample is recognised without decoding it.
- `sound_index.py` and `indexer.py`: The index of how the installed library sounds, built from Ableton's own preview renders, and the ranking `match_sound` answers with. `indexer.py` is also the `live-maestro-index` entry point.
- `transcribe.py`: Runs basic-pitch in a separate interpreter and hands back note events. The transcriber supports Python up to 3.11 and this server runs later, so it is a subprocess and not an import.
- `instruments.py`: Intent to instrument lookup over Live's browser, which is what `find_sound` answers with.
- `automation.py`: Curve generation, interpolation algorithms, and envelope sampling.
- `lookup.py`: Intent to path lookup over the catalog: routing, lexical ranking, and family collapse.
- `vocabulary.py`: Studio vocabulary (route tables and synonyms, read by `lookup.py`). Pure modules: no socket, no Live.
- `maxgen.py` and `probe_protocol.py`: The Max for Live patcher generator, and the wire contract of the throwaway device the spikes used. The analyzer device and the probe device are both generated rather than hand-built, so the file in the repository cannot disagree with the contract the client holds.
- `installer.py`: Puts the Remote Script and the analyzer device into the User Library. Ships in the wheel and is what `live-maestro-install` runs; `scripts/install_script.py` is a wrapper for running it from a checkout.
- `server.py`: FastMCP server exposing tools and resources to AI clients.

Design rule: Transport, path resolution, and musical logic remain isolated. `client.py` has no dependency on musical theory, and `music/` has no dependency on network sockets.

---

## The catalog

The catalog in `src/live_maestro/catalog/*.yaml` serves as the single source of truth for Live's fixed object model surface.

Example entry:

```yaml
- id: track.volume_value
  path: song.tracks[{track}].mixer_device.volume.value
  access: [get, set]
  kind: float
  range: [0.0, 1.0]
  unit: normalized
  display: db
  verify: read_back
  status: verified
  doc: >
    Track volume. The raw value is normalized between 0.0 and 1.0 (where 0.85 corresponds to 0 dB).
    Use display for human-readable decibel output.
  params:
    - {name: track, kind: int, required: true}
```

Catalog files are partitioned by LOM domain (`10-song.yaml`, `20-track.yaml`, `30-clip.yaml`, `40-device.yaml`, `50-browser.yaml`) to keep them organized and easy to maintain.

### Fixed and dynamic surface

The catalog covers static properties defined by Live's core object model. In contrast, third-party VST and AU plugins expose parameters dynamically at runtime only after being configured in Live's device chain. Dynamic parameters are discovered at runtime using `lom_describe` and `lom/introspect.py` rather than hardcoded in the catalog.

---

## Spec and builder (`spec.py`)

`spec.py` defines the data models for catalog specifications:
- `ParamSpec`: Placeholders in paths (e.g., `{track}` index or `{root}` category name).
- `ArgSpec`: Positional arguments for method calls (`access: [call]`).
- `PathSpec`: Complete path description including allowed operations, type definitions, and verification hooks.

`build_path(spec, **kwargs)` interpolates placeholders and validates against LOM grammar. `validate_value(spec, value)` validates data types, boundaries, and enum options before sending data across the socket.

---

## Executor (`executor.py`)

The executor is the central dispatch layer. It enforces safety guards, validates arguments, sends commands over the client socket, maps responses to `Result` objects, and runs verification routines.

```python
def execute(client, registry, spec_id, *, confirm=False, **args) -> Result:
    spec = registry.get(spec_id)
    if spec.destructive and not confirm:
        return Result.blocked_(spec, "destructive operation; requires confirm=True")
    if requested_access not in spec.access:
        return Result.blocked_(spec, "requested operation not permitted by catalog")
    prepared = _prepare(spec, op, args)
    result = _dispatch(client, prepared)
    if spec.verify:
        result.verified = VERIFIERS[spec.verify](client, spec, args, result)
    return result
```

Multi-step operations can be grouped using `execute_batch()`, which sends them in a single `lom_batch` request to reduce round-trip latency.

### Read-back as a principle

When setting parameters via `lom_set`, the response includes `before`, `after`, `clamped`, `changed`, and a `read_back` verdict:

| `read_back` | Description |
|---|---|
| `applied` | The value in Live matches the requested value. |
| `clamped` | Live adjusted or quantized the value to a different valid step. |
| `not_observed` | The value has not changed yet (some properties update asynchronously). |

This ensures that clients receive accurate feedback on whether a value was accepted as-is or modified by Live.

---

## The Remote Script: Handler families

The Remote Script implements eighteen handlers (docs/protocol.md §5): five generic ones,
eleven specialized ones including typed WarpMarker construction, and two that answer on
the client thread without touching a Live object.

### Generic handlers (5 handlers)
- `lom_get`: Reads a property value or list of child handles.
- `lom_set`: Writes a property value and reads back the result.
- `lom_call`: Calls an allowlisted method on a resolved object.
- `lom_describe`: Introspects properties, children, and methods of a live object.
- `lom_batch`: Executes multiple operations in a single main-thread invocation.

### Specialized handlers (10 handlers in 5 families)
- `notes_get` / `notes_set`: Reading, replacing, and appending MIDI note arrays.
- `automation_read` / `automation_write` / `automation_clear`: Sampling, interpolating, and clearing clip envelopes.
- `browser_walk`: Breadth-first searching and ranking of Live's browser graph.
- `events_observe` / `events_drain` / `events_clear`: Event subscription via ring buffers.
- `enum_names`: Reads the member names of a Live enum type, so an integer property's values can be decoded. It fills the catalog, and `lom_enums` exposes the same handler at runtime for a type no row covers.

---

## The MCP surface (`server.py`)

The FastMCP server exposes four access tiers:

1. **Intent Tools:** High-level musical operations (`get_session`, `create_track`, `load_device`, `write_clip_notes`, `set_mix`, `write_automation`, `arrange`, `play`).
2. **LOM Escape Hatches:** Low-level operations (`lom_get`, `lom_set`, `lom_call`, `lom_batch`, `lom_describe`, `lom_enums`) for direct access to any catalogued path. `lom_enums` reads a Live enum type's member names, which is how an integer property's values are decoded.
3. **Lookup:** `find_path` turns an intent in the caller's own words into the mechanism that serves it. See below.
4. **MCP Resources:**
   - `live://catalog`: A map of the catalog: areas, counts, status, and how to search it. About 500 tokens, and deliberately carries no rows.
   - `live://catalog/{selector}`: Full rows for one area, status, or substring.
   - `live://session`: Live snapshot of tracks, clips, and mixer state.
   - `live://limits`: Documentation of known constraints.

### Finding a path without reading the catalog (`lookup.py`, `vocabulary.py`)

The catalog exists so that a handful of tools can stand in for one tool per Live property.
That only works if a caller can find a row without reading the catalog. Reading all 1168
rows out of `live://catalog` costs about 134,000 tokens, and a substring search over
them picks the right mechanism 17 percent of the time.

`find_path` answers the same question for about 1.5k tokens, and answers with a route
rather than only with rows:

| Route | When | What comes back |
|---|---|---|
| `catalog_row` | Live's fixed object model covers it | Ranked rows, mirrored scopes collapsed, with the writable companion named where the best row is a read-only parameter object |
| `device_parameter` | A knob inside a device | The procedure to reach it, because the catalog cannot enumerate what a device exposes |
| `blocked` | `docs/limits.md` says Live cannot do it | A refusal with the manual alternative |
| `unresolved` | Nothing matched | The words that found nothing |

An intent that an existing tool serves better is named in a `tool` field beside the rows
rather than in place of them. A tool name is a better route to the same end, but it is not
the path that was asked for, and about a third of all queries match a tool entry.

Ranking is lexical. The path leaf and the id outweigh the doc's first sentence, which
outweighs the rest of the doc. Rarity separates a term like `crossfader` from one like
`position`. Mirrored rows collapse into one hit whose `scopes` name the other places the
property exists: `track`/`return`/`master`/`chain`, `clip`/`arrangement_clip`,
`device`/`return_device`/`master_device`, and `drum_pad`/`rack_drum_pad`. `vocabulary.py`
carries the route tables that answer the intents rows cannot, and the studio words the
catalog does not use, with every anchor checked against a real row by
`tests/test_lookup.py`.

Ranking selects likely mechanisms and catalog rows from producer vocabulary. It is a lexical routing aid, not a guarantee that the first candidate serves an ambiguous request.

### Tool argument schema definitions (`toolargs.py`)

Tools declare explicit structured input types via `toolargs.py` rather than generic container types (e.g. `list[dict[str, Any]]`). Explicit types ensure the emitted JSON Schema exposes exact field names, data types, and constraints to MCP clients:

- **Shape models:** `NoteIn` and `BatchOp` define field specifications and numeric type coercion for incoming parameters.
- **Inlined schemas:** MCP client implementations often fail to resolve external `$ref` and `$defs` schema references, leading to empty or unconstrained argument objects. `NoteArg` and `BatchOpArg` wrap models with inlined schemas via `WithJsonSchema` so that schema specifications are flattened without runtime indirection.
- **Contract verification:** `tests/test_tool_schema.py` enforces that all tool containers declare concrete shapes and that schema references remain flattened without `$ref` pointers.

---

## Verification (`verify` hooks)

Catalog entries can declare a verification hook to check post-conditions. Every name a
row uses must resolve to a verifier in `executor.py`. `tests/test_catalog.py` fails if one
does not, because a hook with no code behind it advertises a check that never runs.

State hooks, which read the post-condition directly:
- `read_back`: Verifies that the property matches the requested value.
- `envelope_present` / `envelope_absent`: Checks for automation envelope existence.
- `envelope_moves`: Samples an envelope and checks that it is not flat.
- `has_clip` / `no_clip`: Checks clip slot occupancy.
- `playing_state`: The clip is playing or triggered after a launch, and stopped after a
  stop.

Change hooks, which compare against a reading taken before the call:
- `notes_count`: The clip's note count moved as the method implies (unchanged for a
  quantize, higher after a duplication, no higher after a removal).
- `clip_length`: The clip's length moved as the method implies (higher after
  `duplicate_loop`, no higher after `crop`).
- `warp_state`: The warp marker count moved as the method implies, or, for
  `create_audio_clip`, the slot now holds a clip.

A `verify: none` row states that there is no post-condition to check, and the executor
short-circuits it. That is a statement, not a gap.

### Why change hooks need a baseline

A `lom_set` gets its before-value for free: the script reads the property back inside
Live in the same round trip (protocol §5.4). A `lom_call` reply carries no such thing, and
whether `duplicate_loop` worked is not a property of the clip afterwards but the
difference between afterwards and before. So for the three change hooks the executor takes
the reading itself before dispatching and puts it in `Result.before`, which costs one
extra round trip and makes a call row report `before`/`after` the way a set row does.
`verify=False` skips both the baseline and the check.

A hook returns `None` rather than `False` wherever the evidence cannot settle the
question: the baseline was unreadable (warp markers on a MIDI clip), the transport does
not offer the handler, or Live has deferred the effect. A stop that still reads as playing is reported unverified until later state can establish the outcome.

---

## Directory structure

```
src/live_maestro/
  client.py         Socket transport, timeout classes, and retry policy. No framing:
                    the reader reads until the buffer parses (protocol section 2).
  spec.py           PathSpec, ParamSpec, ArgSpec, and validation rules.
  registry.py       Catalog loading, indexing, and lookup functions.
  executor.py       Dispatch engine, safety guards, and verification hooks.
  models.py         Data models, enums, and result containers.
  toolargs.py       Argument models for the nested tool arguments, so the JSON
                    schema a client reads carries their field names and types.
  automation.py     Curve generation, interpolation, and envelope sampling.
  lookup.py         Intent to path lookup: routing, lexical ranking, family collapse.
  vocabulary.py     Route tables and studio synonyms, read by lookup.py.
  instruments.py    Intent to instrument lookup over Live's browser.
  sound_index.py    The index of how the installed library sounds, and its ranking.
  indexer.py        Builds that index. Also the live-maestro-index entry point.
  transcribe.py     Runs basic-pitch in a sidecar interpreter, returns note events.
  installer.py      Installs the Remote Script and the analyzer device.
  maxgen.py         Generates Max for Live devices: patcher graph, container, CLI.
  probe_protocol.py Wire contract of the throwaway device the spikes used.
  catalog/*.yaml    Declarative catalog files grouped by domain.
  lom/
    paths.py        LOM path grammar parser and validation helpers.
    introspect.py   Runtime object inspection and session snapshot builders.
  audio/
    decode.py       Any audio file to mono samples at the analysis rate.
    descriptor.py   What a piece of audio sounds like, as comparable numbers.
    sample_header.py Container metadata, standard library only, no decoding.
  analyzer/
    protocol.py     The OSC wire contract and its codec. Nothing else holds a copy.
    client.py       The socket. The only file here that needs Live running.
    metrics.py      Everything derived from a reply, as pure functions.
    report.py       The choosing and the wording of the answer.
    models.py       What analyze_audio returns.
  music/
    notes.py        MIDI note manipulation and formatting.
    theory.py       Musical scales, chord voicings, and transposition.
    humanize.py     Timing swing and velocity variations.
    launch.py       When a fired clip or scene will actually start.
    transcription.py Cleaning up what a transcriber heard, before it is written.
  als/
    read.py         Parser for saved .als project and .adg preset files.
    write.py        Direct modifier for .als project files.
  server.py         FastMCP server implementation.

live-remote-script/
  __init__.py       Remote Script running inside Live.

scripts/
  install_script.py    Installs the Remote Script and analyzer device from a checkout.
  sync_catalog.py      Reconciles catalog entries against a running Live instance.
  probe_paths.py       Validates specific paths and updates catalog status.
  write_measurements.py Folds probe runs into the report the catalog is checked against.
  inventory.py         Surveys the open set and the local installation.
  build_analyzer_device.py Generates the Max device that answers analyze_audio.
  build_probe_device.py    Generates the throwaway device the spikes used.
  build_sound_index.py     Wrapper around live_maestro.indexer, for a checkout.
  eval_sound_index.py      Scores the sound index against the categories it carries.
  eval_transcription.py    Scores the transcription cleanup against a MIDI export.
  spike_probe.py           Drives the probe device and answers spikes 1 and 2.
  stress_load_delete.py    A thousand load and delete cycles, for endurance.
```

Nothing in `audio/`, `music/` or `analyzer/` except `analyzer/client.py` opens a socket or needs Live. That is what makes the sound matching, the transcription cleanup and every threshold in an audio verdict testable on a machine with no DAW on it.
