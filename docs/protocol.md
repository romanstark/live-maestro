# Wire Protocol: Live Maestro

This document specifies the socket protocol between the external Python client (`src/live_maestro/client.py`) and the Remote Script running inside Live (`live-remote-script/__init__.py`). Sections 1 to 10 cover that protocol, which is Channel A. Section 11 covers the separate protocol Channel C uses to reach its Max for Live device.

Companion documents: [architecture.md](architecture.md) for system design, [catalog.md](catalog.md) for the path catalog schema, and [limits.md](limits.md) for API constraints.

---

## 1. Transport

| Setting | Value | Notes |
|---|---|---|
| Socket | TCP | Bound to `127.0.0.1` (loopback only) |
| Port | `9878` | Configurable |
| Encoding | UTF-8 JSON | One JSON object per message |
| Framing | None | Stream-based JSON parsing (see §2) |
| Concurrency | Strictly serial | One outstanding request per socket (see §3) |
| Authentication | None | Secured by loopback interface binding |

Binding strictly to `127.0.0.1` ensures that communication remains internal to the host machine.

---

## 2. There is no framing

Messages do not use delimiters or length prefixes. Message boundaries are determined by parsing JSON objects directly from the incoming byte stream.

Both the client and server implement the following decoding loop:

```python
buffer = ""
while True:
    buffer += socket.recv(8192).decode("utf-8")
    try:
        obj, end = json.JSONDecoder().raw_decode(buffer)
    except ValueError:
        continue  # Incomplete message, read more bytes
    buffer = buffer[end:].lstrip()  # Retain unparsed buffer remainder
    process_message(obj)
```

Implementation considerations:
- Use an incremental UTF-8 decoder to avoid issues with multi-byte characters split across `recv()` calls.
- Retain unparsed data after `raw_decode` because multiple responses can arrive in a single `recv()` call.
- The script caps one client's unparsed buffer at 8 MiB. Past that it discards the buffer and answers `bad_path` with "buffer discarded", because there is no delimiter to resynchronise on. A chunk that is not valid UTF-8 answers `type_error` and the script closes the connection.

---

## 3. Strictly serial

The connection handles exactly one request at a time. Sending a second request before the first response has been received will desynchronize the stream.

The client manages this using a mutex lock across send-and-receive cycles. When multiple operations need to be executed efficiently, use [`lom_batch`](#57-lom_batch) to package them into a single round trip.

If a timeout occurs, the client closes and reopens the socket to ensure no stale data remains in the buffer. Automatic retries upon reconnect are limited to read-only handlers (see §8).

---

## 4. Message shape

### Request Format
```json
{
  "type": "lom_get",
  "params": {
    "path": "song.tempo"
  }
}
```

### Response Format (Success)
```json
{
  "status": "success",
  "result": {
    "path": "song.tempo",
    "value": 124.0,
    "type": "float"
  }
}
```

### Response Format (Error)
```json
{
  "status": "error",
  "code": "no_such_path",
  "message": "Track has no attribute 'volume'",
  "path": "song.tracks[0].volume"
}
```

`params` is always a JSON object. `result` is always a JSON object rather than a raw primitive or array, allowing fields to be added without breaking existing parsers.

### Error Codes

| `code` | Description |
|---|---|
| `unknown_handler` | Handler type not recognized |
| `bad_path` | Path syntax is invalid |
| `no_such_path` | Attribute or index does not exist on the target object |
| `index_out_of_range` | Collection index exceeds current length |
| `not_settable` | Property exists but is read-only in the LOM |
| `method_not_allowed` | Method is not included in the allowlist |
| `type_error` | A value, an argument or the request itself has the wrong type or shape |
| `live_error` | Live raised an internal error |
| `internal` | Script execution error |
| `skipped` | Only inside `lom_batch`: an earlier op failed and `atomic` was requested (see §5.7) |

---

## 5. Handlers

The Remote Script implements 18 handlers across generic and specialized families.

### 5.1 `ping`
- **Request:** `{"type": "ping", "params": {}}`
- **Response:** `{"pong": true, "script_version": "<SCRIPT_VERSION>", "uptime": 123.45}`

The version in these examples is a placeholder on purpose. `SCRIPT_VERSION` in
`live-remote-script/__init__.py` is the only place it is stated. A literal here would
be wrong one release later.

The ping handler operates independently of Live's main thread and LOM locks, allowing liveness checks even when Live is executing modal operations.

### 5.2 `script_info`
- **Request:** `{"type": "script_info", "params": {}}`
- **Response:**
  ```json
  {
    "name": "live-maestro",
    "script_version": "<SCRIPT_VERSION>",
    "protocol_version": 1,
    "handlers": ["lom_get", "lom_set", "..."],
    "live_version": "12.4.5",
    "host": "127.0.0.1",
    "port": 9878,
    "allowlist_size": 144
  }
  ```

`protocol_version` indicates the wire format specification. `live_version` is cached at startup.

### 5.3 `lom_get`
- **Request:** `{"type": "lom_get", "params": {"path": "song.tracks"}}`
- **Response:**
  ```json
  {
    "path": "song.tracks",
    "type": "list",
    "class": "Vector",
    "count": 4,
    "value": [
      {"__lom__": "Track", "path": "song.tracks[0]", "name": "Drums"},
      {"__lom__": "Track", "path": "song.tracks[1]", "name": "Bass"}
    ]
  }
  ```

LOM collections (Vectors) are returned as lists of element handles with individual paths. `count` is the collection's own length and not the length of `value`: encoding stops at 512 elements, and a reply that hit the cap adds `truncated: true` and `list_cap`. A `lom_get` that lands on a DeviceParameter also carries `display`.

### 5.4 `lom_set`
- **Request:** `{"type": "lom_set", "params": {"path": "song.tempo", "value": 128.0}}`
- **Response:**
  ```json
  {
    "path": "song.tempo",
    "requested": 128.0,
    "before": 120.0,
    "after": 128.0,
    "read_back": "applied",
    "clamped": false,
    "changed": true,
    "read_back_attempts": 1
  }
  ```

`lom_set` reads back the property after writing to verify the change:
- `applied`: Target value matches requested value.
- `clamped`: Live quantized or clamped the value to a valid step.
- `not_observed`: The property still reads the value it held before the write. Either Live clamped to the value already stored or the write has not been applied yet, and from inside the writing task the two are indistinguishable. A second read is taken one main-thread tick later where the scheduler allows it: the reply carries `read_back_attempts`, and a second read adds `first_read`.

### 5.5 `lom_call`
- **Request:**
  ```json
  {
    "type": "lom_call",
    "params": {
      "path": "song.tracks[0].clip_slots[0].clip",
      "method": "clear_envelope",
      "args": [{"__path__": "song.tracks[0].devices[0].parameters[3]"}]
    }
  }
  ```

Arguments using `{"__path__": "<path>"}` resolve to existing Live objects before method execution.

### 5.6 `lom_describe`
- **Request:** `{"type": "lom_describe", "params": {"path": "song.tracks[0].devices[0]"}}`
- **Response:**
  ```json
  {
    "path": "song.tracks[0].devices[0]",
    "class": "PluginDevice",
    "name": "Wavetable",
    "properties": [{"name": "is_active", "type": "bool", "settable": true, "value": true}],
    "children": [{"name": "parameters", "type": "list", "class": "Vector", "is_collection": true, "count": 32, "path": "song.tracks[0].devices[0].parameters"}],
    "methods": ["get_parameter_names", "store_chosen_bank"],
    "allowed_methods": ["get_parameter_names"]
  }
  ```

Inspects the properties, child collections and methods of any live object. `depth` is optional, defaults to 1 and is clamped to 4. `settable` is `true` or `false` only where Python can tell and is `null` otherwise, so the catalog stays the authority on writability. Each child collection carries `class`, `is_collection` and a real `count`, which is what makes "how many tracks are there" a single round trip. `methods` lists every callable the object has, and `allowed_methods` lists the shorter set `lom_call` will dial.

### 5.7 `lom_batch`
- **Request:**
  ```json
  {
    "type": "lom_batch",
    "params": {
      "ops": [
        {"op": "set", "path": "song.tracks[0].mixer_device.volume.value", "value": 0.85},
        {"op": "set", "path": "song.tracks[1].mixer_device.volume.value", "value": 0.75}
      ],
      "atomic": false
    }
  }
  ```
- **Response:** `{"results": [...], "ok_count": 2, "error_count": 0, "atomic": false, "rolled_back": false}`

Each entry in `results` is that operation's own unwrapped result with a `status` field added, flat rather than nested under `result`. A `get` therefore returns `{"path": ..., "value": ..., "type": ..., "status": "success"}`, and a failed operation carries `status: "error"` with the section 4 code alongside it.

Executes multiple operations in a single round trip. An `op` is `get`, `set` or `call` and nothing else, and any other value answers `unknown_handler` for that entry. `atomic` is advisory, not a transaction: the LOM has no rollback, so operations that already ran stay applied. With `atomic: true` the batch stops at the first error and the remaining results carry the code `skipped`. `rolled_back` is always `false`. A `set` inside a batch never takes the second read a lone `lom_set` takes, because the batch holds Live's main thread until every op is done, so the same write can answer `not_observed` here and `applied` on its own.

The wire handler sets no upper bound on the number of operations in `ops`. The cap a caller meets through the MCP tools is a tool-layer policy (`MAX_BATCH_OPS` in `src/live_maestro/server.py`), enforced before the request is built, and a client that speaks this protocol directly does not get it.

Batch scheduling overhead depends on the operation and host. The handler's main-thread budget is `min(18 s, 4 s + 0.4 s per operation)`; read-batch timings do not establish a safe throughput for writes or calls.

### 5.8 `notes_get` / `notes_set`
- **`notes_get`:**
  - `params: {path, from_time?, time_span?, count_only?}`
  - Returns MIDI note specifications within the specified time window.
- **`notes_set`:**
  - `params: {path, notes: [...], mode: "replace" | "append"}`
  - Default mode is `"replace"`. It removes every note in the clip (pitches 0 to 127, from beat 0 to the end of the clip or of the incoming notes, whichever is later) and then inserts the new list. There is no windowed replacement, and the LOM has no rollback: if the insert fails after the removal, the notes are gone.

Note structure:
```json
{
  "pitch": 60,
  "start_time": 0.0,
  "duration": 0.5,
  "velocity": 100,
  "mute": false,
  "probability": 1.0,
  "velocity_deviation": 0.0,
  "release_velocity": 64
}
```

`notes_get` adds one key the structure above does not list: **`note_id`**, Live 11+'s own handle for an existing note. Read notes carry this identity separately from their content. It is identity rather than content and is never written back: the `write_clip_notes` tool drops it on input and reports it as `input_keys_ignored`, so a list read from a clip can be edited and written back unchanged. Live may also return the MPE per-note keys `pitch_bend_range`, `pressure`, `timbre` and `slide`. These are treated the same way. Any key outside both sets is refused rather than dropped.

### 5.9 `automation_read` / `automation_write` / `automation_clear`
- **`automation_read`:**
  - `params: {path, parameter, start?, end?, points?}`
  - Samples parameter curve values across the clip length.
- **`automation_write`:**
  - `params: {path, parameter, points: [...], resolution?, interpolation?, exponent?}`
  - Generates and writes breakpoints. Four curves exist: `linear`, `hold`, `exponential` and `logarithmic`. Twelve spellings reach them, matched case-insensitively with `-` and space read as `_`: `linear`, `lin`, `hold`, `step`, `constant`, `none`, `exponential`, `exp`, `ease_in`, `logarithmic`, `log` and `ease_out`. `ease_in` resolves to `exponential` and `ease_out` to `logarithmic`, and the canonical name is reported back. Anything else answers `type_error`.
- **`automation_clear`:**
  - `params: {path, parameter?, all?}`
  - Removes envelope data for the named parameter. With `parameter` omitted, or with `all: true`, the whole clip is cleared through `Clip.clear_all_envelopes`. The reply carries `cleared_all` and `has_envelopes`.

### 5.10 `browser_walk`
- **Request:** `{"type": "browser_walk", "params": {"root": "drums", "query": "909", "limit": 10}}`
- **Response:** An object, never a bare list. Four request shapes share one walk and `mode` names the one that ran: `uri` resolves a single item and reports where it sits, `path` navigates `Folder/Subfolder` below one root, `query` answers `matches` ranked best first with `count`, `considered`, `matched` and `depth_reached`, and the default `tree` walks the roots `depth` levels. Every shape reports `truncated` and `truncated_by`, because a walk that ran out of budget and a walk that found nothing are different answers.

Every shape also reports `status`, and the three that walk the roots (`uri`, `query` and the default `tree`) report `unreached_roots` beside it. `status` is `complete` only when nothing stopped the walk short and every root was walked to the end; otherwise it is `incomplete` and the answer is a lower bound, so `count: 0` together with `status: "incomplete"` does not mean the item is absent. `unreached_roots` names the roots whose subtree was **not fully searched**. That includes a root the walk entered and left half walked, not only one it never opened: the part that was skipped is where the answer may be. Pass such a name back as `root` to search that category on a budget of its own. Four details follow from how the walk works. `status` is about the walk that was asked for, so a request that sets `depth` narrows the search space and a walk that covered that space reports `complete`. A `uri` hit reports `status: "complete"` with an empty `unreached_roots`, because the walk stops on the item it was looking for and the roots it did not finish are not a gap in that answer. `path` mode carries `status` without `unreached_roots`, because it walks one named folder rather than the roots. And `truncated` is not `status`: the `limit` a caller asked for cuts the ranked list after the walk is over, so a `query` cut by it alone answers `truncated: true` with `truncated_by: "limit"` and `status: "complete"`. `truncated_by: "match_pool"` is the cut that looks the same and is not: collection stops there, so the walk really did stop short and `status` reads `incomplete`. Where a walk filled the pool and the caller's `limit` cut the list as well, `truncated_by` names `limit` and only `status` and `unreached_roots` still report the shortfall, which is why coverage is read off those two and never off `truncated_by`. In the other three modes `truncated` comes from the budget alone, and `complete` beside `truncated: true` is a pairing they never report.

Performs breadth-first searches of Live's internal library. Every entry carries a `kind`: `device`, `preset`, `sample`, `folder`, `other` or `unknown`. The optional `kind` filter accepts only the first four plus `any`, so filtering cannot reach an `other` or `unknown` entry.

### 5.11 `events_observe` / `events_drain` / `events_clear`
- **`events_observe`:** `params: {path, property}`. Registers a Live listener for that property and feeds a ring buffer of 2000 events. The reply carries `already_registered` and `listener_count`. A property with no `add_<name>_listener` answers `no_such_path`: not every LOM property is observable.
- **`events_drain`:** `params: {max?}`. Returns and removes queued events, with `dropped` counting what the buffer overwrote since the last drain and `remaining` what is left. It reads only the script's own buffer, so it answers while Live's main thread is busy.
- **`events_clear`:** `params: {path?, property?}`. With both, only that listener is removed. With neither, every listener is removed and the buffer is emptied as well.

### 5.12 `enum_names`
- **Request:** `{"type": "enum_names", "params": {"type": "Song.Quantization"}}`
- **Response:** `{"type": "Song.Quantization", "members": {<name>: <int>}, "class": "..."}`. With `type` omitted the reply is `{"types": [...]}`, the enum-like containers this Live build has, which is how a catalog row is mapped to a type rather than guessed. The handler reads module attributes under `Live` and touches no LOM object.

---

### 5.13 `warp_markers_set`

Request: `{"type":"warp_markers_set","params":{"path":"song.tracks[0].arrangement_clips[0]","markers":[{"seconds":0,"beat":0},{"seconds":10,"beat":20}],"replace":true}}`.

The clip must be audio. The 2-2000 points start at zero, increase on both axes and
fit the source duration. Actual `Live.Clip.WarpMarker` values are constructed and
their fields checked before mutation. The handler enables Warp, removes other real
markers when replacing, retains Live's final shadow and reads back the whole map.
The reply reports `verified`, `missing`, `extra`, `before` and `shadow_marker`.
An error after mutation reports `partial` and `before`; writes are never retried.

`script_info` additionally advertises `warp_marker_constructor` and bounded native
`file_method_docs` diagnostics. Older scripts lack these optional fields/handler;
the public setter requests a local script update before any unsupported write.

`lom_set` can clear the measured nullable browser target with
`{"path":"app.browser.hotswap_target","value":{"__path__":null}}`.
This explicit null reference is limited to that property.

---

## 6. The resolver, and why calls need an allowlist

Paths are evaluated starting from root objects (`song`, `app`):

```
song.tracks[3].mixer_device.volume
 |    |         |            |
root  list      attribute    attribute
```

Path rules:
- Grammar format: `segment ("." segment)*` where `segment = name | name "[" int "]"`.
- Indices must be non-negative integers.
- Property access is permitted dynamically through path traversal.
- Method calls are permitted only via `lom_call` and must be present in the Remote Script's allowlist.
- Group tracks are guarded during track iterations because they lack arming state and arrangement clips.

---

## 7. Values

Primitive JSON values are passed directly. LOM objects are represented as handle dictionaries:

```json
{"__lom__": "Track", "path": "song.tracks[3]", "name": "Bass"}
```

`path` is the key a handle carries when Live hands it back. `__path__` is the key that passes a handle the other way, as a method argument (§5.5). A reply never carries `__path__`, and an argument is never written as `path`.

Parameter value scales:
- Most continuous Live parameters are normalized between 0.0 and 1.0.
- Decibel and frequency readings from `str_for_value()` are provided in the `display` field.
- Quantized parameters (`is_quantized: true`) accept discrete steps listed in `value_items`.

---

## 8. Timeouts and retries

| Category | Default Timeout | Connection Loss Retry |
|---|---|---|
| Read operations (`ping`, `script_info`, `lom_get`, `lom_describe`, `notes_get`, `automation_read`, `browser_walk`, `events_drain`, `enum_names`) | 10 seconds | Retries once on reconnect |
| Everything else (`lom_set`, `lom_call`, `lom_batch`, `notes_set`, `automation_write`, `automation_clear`, `events_observe`, `events_clear`, `warp_markers_set`) | 20 seconds | Never retried automatically |

All eighteen handlers are in one row or the other. The classification lives in `READ_ONLY_HANDLERS` in `client.py`, and a handler left out of it is treated as a write: it gets the longer timeout and no retry.

These are the client's limits. The script holds its own, shorter, per-handler budget for the time a request may occupy Live's main thread (`_MAIN_THREAD_TIMEOUTS`: 8 seconds by default, 9 for `browser_walk`, 15 for `lom_call`, `notes_set` and `automation_clear`, 18 for `automation_write`), and the browser walk and the envelope write loop hold budgets under that again. The innermost limit fires first, so a request that runs long usually answers with a truncated or partial result rather than with a client timeout.

To avoid duplicate actions (such as appending notes twice), write operations that time out are not retried automatically. The calling client can perform a read operation to verify whether the change landed.

---

## 9. Changing the script costs a restart

Live compiles and caches Remote Scripts on startup. Modifications to `live-remote-script/__init__.py` require:
1. Copying the script to `<User Library>\Remote Scripts\LiveMaestro\__init__.py`.
2. Deleting any `__pycache__` folder in that directory.
3. Restarting Live.

---

## 10. What a successful response proves

A response with `"status": "success"` confirms that the Remote Script executed without raising an exception. For `lom_set`, what is stored is whatever `read_back` names: `applied` means the requested value is in memory, `clamped` means Live stored a different value, and `not_observed` means the property still reads its previous value and nothing about the write has been proved.

The MCP tool layer refuses arbitrary writes to
`song.tracks[i].arrangement_clips[j].end_marker`; the wire handler does not impose
that guard. A direct protocol client must read `end_time` and verify resulting clip
geometry separately from a stored marker value. The specialized audio-import
workflow manages these markers and checks duration.

It does not indicate:
- Whether the result is audible (e.g., if a track is muted or an effect is bypassed).
- Whether the change has been saved to the `.als` project file on disk.
- Whether the change will persist across an undo action.

---

## 11. The analyzer protocol (Channel C)

A second wire protocol reaches the Max for Live device `analyze_audio` measures with. It shares nothing with the one above: different transport, different encoding, different port.

| Setting | Value | Notes |
|---|---|---|
| Socket | UDP | Bound to `127.0.0.1` (loopback only) |
| Port | `9882` | The device binds it. Overridable with `LIVE_MAESTRO_ANALYZER_PORT` |
| Encoding | Open Sound Control | int32, float32 and string only |
| Concurrency | One request, any number of replies | Every instance in the set answers |
| Authentication | None | Secured by loopback interface binding |

The encoding is OSC because that is what Max's own objects speak. `udpsend`'s reference page states that Max messages are serialized as OSC-compatible UDP packets, and `udpreceive` decodes OSC and outputs Max messages. A raw JSON datagram sent at `udpreceive` is not the protocol it implements.

The client socket must not be connected. A connected UDP socket drops every datagram whose source is not the address it connected to, and the device answers from `udpsend`'s own ephemeral port rather than from the port it listens on.

### 11.1 The contract lives in one file

`src/live_maestro/analyzer/protocol.py` holds the port, both OSC addresses, the field order below, the band edges, the averaging ceiling and the schema number. `scripts/build_analyzer_device.py` generates the device from it, and `live_maestro.analyzer.client` decodes with it, so the two ends cannot name different ports or read fields in different orders. Nothing else may hold a copy of any of these values.

### 11.2 Request

Address `/maestro/measure`, four int32 arguments in this order:

| # | Argument | Type |
|---|---|---|
| 0 | `request_id` | int32 |
| 1 | `window_ms` | int32 |
| 2 | `reset` | int32 |
| 3 | `reply_port` | int32 |

`reply_port` is the ephemeral port the client bound. The device aims its `udpsend` at it with a `port` message before answering, so neither side carries a hard-coded reply port. `reset` restarts everything the meters hold.

### 11.3 Reply

Address `/maestro/report`, one packet per instance, arguments in this order:

| # | Field | Type | Meaning |
|---|---|---|---|
| 0 | `schema` | int32 | Reply layout version. A client refuses any number but its own. |
| 1 | `request_id` | int32 | Echoes the request, so a late answer to an abandoned request is dropped. |
| 2 | `samplerate` | int32 | Sample rate Live is running at, from `dspstate~`. |
| 3 | `window_samples` | int32 | What the averaged fields cover, clamped to the buffer size. |
| 4 | `momentary_lufs` | float32 | `loudness~` outlet 0. Loudness of the last 400 ms. |
| 5 | `short_term_lufs` | float32 | `loudness~` outlet 1. Loudness of the last 3 s. |
| 6 | `integrated_lufs` | float32 | `loudness~` outlet 2. Gated loudness since the last reset. |
| 7 | `lra_lu` | float32 | `loudness~` outlet 3. Loudness range over the integration. |
| 8 | `sample_peak_db` | float32 | `loudness~` outlet 4. Reports half the amplitude (see [limits.md](limits.md)). |
| 9 | `true_peak_db` | float32 | `loudness~` outlet 5. Oversampled, and exact. |
| 10 | `rms_l` | float32 | Left channel RMS over the window. |
| 11 | `rms_r` | float32 | Right channel RMS over the window. |
| 12 | `mean_lr` | float32 | Bipolar average of the two channels multiplied, which a correlation needs. |
| 13 | `rms_low_l` | float32 | The same for the left channel below 120 Hz. |
| 14 | `rms_low_r` | float32 | The same for the right channel below 120 Hz. |
| 15 | `mean_low_lr` | float32 | The same product average below 120 Hz. |
| 16 | `band_sub` | float32 | RMS of 20 Hz to 60 Hz on the mono sum. |
| 17 | `band_bass` | float32 | RMS of 60 Hz to 250 Hz. |
| 18 | `band_low_mid` | float32 | RMS of 250 Hz to 500 Hz. |
| 19 | `band_mid` | float32 | RMS of 500 Hz to 2 kHz. |
| 20 | `band_high_mid` | float32 | RMS of 2 kHz to 6 kHz. |
| 21 | `band_presence` | float32 | RMS of 6 kHz to 10 kHz. |
| 22 | `band_air` | float32 | RMS above 10 kHz. A high pass with no upper edge. |
| 23 | `cal_momentary_lufs` | float32 | The reference sine's momentary loudness. |
| 24 | `cal_sample_peak_db` | float32 | What outlet 4 reports for the reference. |
| 25 | `cal_true_peak_db` | float32 | What outlet 5 reports for the reference. |
| 26 | `track_id` | int32 | Live Object Model id of the track this instance sits on. |
| 27 | `track_name` | string | Name of that track. The only string, and last, so a split symbol can be rejoined. |

Every numeric field is a raw measurement. No dB value, correlation, crest factor, centre frequency or verdict crosses this wire: those are derived in `analyzer/metrics.py` and `analyzer/report.py`, which is what makes them testable without Live. The averaged fields are amplitudes, because `average~` in RMS mode reports the root of the mean square.

Strings come last. A track name is a symbol the user typed and may hold spaces, and a serializer is free to split it. Keeping every number in front of it means a split tail can be rejoined without losing a measurement.

### 11.4 Schema

`schema` is the first field of every reply. A client that reads a different number refuses the reply rather than reading it against the wrong layout, and reports `schema_mismatch` with the numbers it saw. Any change to the field order or the field set raises `REPLY_SCHEMA` in the same commit, and the device is regenerated and reloaded in Live.

### 11.5 The reference signal

The device generates a sine of 1000 Hz at amplitude 0.5, whose peak is therefore -6.0206 dBFS by construction, and feeds it to a second `loudness~` that reaches nothing else. The three `cal_` fields are what that meter reports for it. That is what makes the sample peak correction a measurement taken on the same call, at the sample rate Live is running, rather than a constant. [limits.md](limits.md) records what each peak outlet does with it.

### 11.6 What a reply proves

A reply proves what the signal measured as it passed the point in the chain where the device sits. It does not indicate:
- Anything about a track the device is not on. Every instance answers every request, and the client picks by track name.
- The position of that track. The device reports a name and an id, never an index, so an index is resolved over Channel A ([limits.md](limits.md)).
- Anything about the room, the monitors or what a listener hears.
