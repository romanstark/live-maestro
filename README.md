# Live Maestro

> An AI copilot for **Ableton Live™**. Speak in natural language to program drums, generate chord progressions, tweak synthesizers, shape automation, and mix your tracks, right inside your open Live set.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE) [![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/) [![Ableton Live 11+](https://img.shields.io/badge/Ableton%20Live-11%20%7C%2012-orange.svg)](https://www.ableton.com/) [![No telemetry](https://img.shields.io/badge/telemetry-none-brightgreen.svg)](#your-music-stays-yours) [![Glama](https://glama.ai/mcp/servers/romanstark/live-maestro/badges/score.svg)](https://glama.ai/mcp/servers/romanstark/live-maestro)

---

## Stay in the creative flow

Making music in a DAW is inspiring, but clicking around to draw notes, adjust mixer faders, and tweak envelopes over and over can pull you out of the zone.

**Live Maestro** acts like an assistant producer sitting next to you in the studio:
- **Keep your hands on the music:** Live stays open right in front of you. You listen, evaluate, and make the creative decisions.
- **Natural studio dialogue:** Ask for beats, basslines, filter sweeps, sidechain setups, or mix adjustments in plain language.
- **Real parameters, real clips:** Nothing is faked or pre-rendered. Everything is written directly into your active project as editable MIDI notes, native devices, and standard Live automation envelopes.

---

## How it feels in the studio

Here is what collaborating with Maestro looks like in a session:

### 1. Beatmaking & Groove
> **You:** *"Build me a driving 126 BPM techno groove. Punchy 909 kick, a crisp clap on 2 and 4, and rolling 16th closed hats with a bit of velocity groove and swing."*
>
> **Assistant:** Created track **Techno Drums** with a 909 Drum Rack and a 4-bar looped clip at 126 BPM:
> - **Kick:** Four-on-the-floor on C1 with accented downbeats.
> - **Clap:** Sitting on beats 2 and 4 on D#1.
> - **Hi-Hats:** 16th notes on F#1 with alternating velocity dynamics (92 / 76 / 86 / 76) and the offbeats pushed late for a swung feel.
>
> Clip is ready and playing.

### 2. Sound Design & Automation
> **You:** *"The hats feel too loud and harsh. Drop them by 2.5 dB, add an Auto Filter to the lead synth, and draw a 4-bar low-pass sweep that opens up towards the drop."*
>
> **Assistant:** 
> - Reduced the Hi-Hat chain volume by **2.5 dB**.
> - Added an **Auto Filter** (Low-Pass) to track **Lead Synth**.
> - Created a 4-bar automation envelope on the filter cutoff frequency, sweeping from **600 Hz** smoothly up to **18 kHz**.

### 3. Harmony & Chords
> **You:** *"Add a lush Neo-Soul chord progression in D minor on an Electric piano track. Keep the chords laid back with realistic human velocity."*
>
> **Assistant:** 
> - Created a MIDI track **Electric Piano** with a Rhodes preset.
> - Wrote a 4-bar progression (*Dm9, G13, Cmaj9, A7#9*) with slight timing humanization and soft velocities between 65 and 85.

---

## What you can do

| Workflow | Example prompt | What Maestro does |
|---|---|---|
| **🥁 Drums & Beats** | *"Create a 4-bar UK Garage drum beat with swung hats and ghost snares"* | Creates drum tracks, loads kits, places MIDI notes with realistic velocities and swing |
| **🎹 Chords & Melodies** | *"Write an 8-bar melodic bassline in F minor that follows the root notes"* | Generates chord progressions, basslines, melodies, and arpeggios |
| **🎛️ Sound Design** | *"Open the Wavetable filter cutoff to 65% and increase resonance slightly"* | Adjusts native instruments, synthesizers, and audio effect parameters |
| **📈 Automation & Envelopes** | *"Draw a 2-bar reverb swell into the pad clip, then copy the clip to bar 31"* | Draws parameter curves, filter sweeps, and volume swells into Session clip envelopes, which travel with the clip onto the timeline |
| **🎚️ Mixing & Levels** | *"Turn the bass down 3 dB, pan the rhythm guitar 25% left, and add a chorus"* | Sets track volumes, panning, sends, returns, and insert effects |
| **🎼 Arrangement & Structure** | *"Duplicate the verse clip to bar 17 in the Arrangement and drop a locator called Drop"* | Copies Session clips onto the arrangement timeline, sets cue points/locators, loops sections |
| **🎚️ Editing & Groove** | *"Transpose the synth lead up a minor third and quantize to 1/16 notes at 70%"* | Transposes pitches, quantizes timing, modifies note lengths, and adjusts velocities |

---

## Three channels into Live

Maestro reaches Live through three complementary routes ([docs/architecture.md](docs/architecture.md)):

- **Channel A: Real-Time Live Object Model (LOM):** A loopback TCP connection (`127.0.0.1:9878`) to the custom Remote Script inside Live's process. Drives Session clips, MIDI note editing, device knobs, mixer levels, and real-time playback.
- **Channel B: Offline Project Files (`.als`):** Direct inspection and safe modification of saved `.als` project files on disk when the target set is closed in Live (Live may continue running with another or blank set). Handles track automation, sidechain routing audits, and third-party VST parameter configuration, protected by automatic timestamped backups and hash validation.
- **Channel C: Audio Measurement (`analyze_audio`):** A Max for Live device (`devices/LiveMaestro_Analyzer.amxd`) placed on any track, answering one OSC request per call over loopback UDP (`127.0.0.1:9882`). Reports EBU R128 loudness, oversampled true peak, stereo correlation broadband and below 120 Hz, and energy in seven bands. Optional: without the device every other tool works and this one reports `device_not_found` with the steps to load it.

---

## MCP Tools & Resources

Maestro exposes **38 tools** and 4 resources over the Model Context Protocol (MCP):

### Intent Tools (Production Workflows)
- **Session & Inspection:** `get_session`, `get_track`, `get_clip`, `get_devices`, `describe`
- **Tracks & Devices:** `create_track`, `delete_track`, `load_device`, `delete_device`, `set_parameter`, `set_parameter_display`
- **Clips & MIDI Notes:** `create_clip`, `delete_clip`, `write_clip_notes`, `read_clip_notes`, `quantize_clip`, `transpose_clip`
- **Mixing & Automation:** `set_mix`, `write_automation`, `read_automation`, `clear_automation`
- **Audio Measurement (Channel C):** `analyze_audio`
- **Arrangement & Transport:** `arrange`, `set_locator`, `set_arrangement_time`, `play`, `stop`, `set_tempo`, `set_loop`
- **Offline Projects (Channel B):** `als_read`, `als_write`

### Smart Path Discovery: `find_path`
Looking up any of Live's 1,166 addressable paths by reading the whole catalog costs ~134,000 tokens. The `find_path` tool maps producer vocabulary (*"turn the bass down"*, *"filter cutoff"*, *"quantize swing"*) into the right mechanism and ranked catalog rows in ~1.5k tokens, distinguishing between fixed LOM rows, runtime device parameters, and impossible requests.

### Generic LOM Escape Hatches
Direct, low-level access to the entire verified catalog surface:
- `lom_get` / `lom_set`: Read or write any catalogued property with read-back confirmation.
- `lom_call`: Invoke allowlisted Live Object Model methods.
- `lom_batch`: Execute multiple operations in a single round trip to eliminate socket latency.
- `lom_describe`: Deep runtime inspection of Live objects, collections, and device chains.
- `lom_enums`: Inspect and decode Live integer enum members.

### MCP Resources
- `live://session`: Live snapshot of active tracks, clips, and mixer state.
- `live://catalog`: Map of all catalog areas, counts, and status indicators.
- `live://catalog/{selector}`: Query catalog rows by area, status, or search term.
- `live://limits`: Documentation of known API and DAW constraints ([docs/limits.md](docs/limits.md)).

---

## Safety & Two-Way Verification

A command sent to Live can be refused, clamped, or applied late, and a plain success reply shows none of that.

- **Safe Dry-Run by Default:** Destructive and structural operations (`delete_track`, `delete_device`, `delete_clip`, `clear_automation`, `als_write`) require explicit `confirm=True`. Without confirmation, Maestro performs a safe dry run and reports exactly what would be removed or altered.
- **Read-Back Verification:** Parameter updates via `lom_set` read the stored value back from Live and report the exact state: `applied` (exact match), `clamped` (quantized or bounded by Live), or `not_observed` (deferred or asynchronous update).
- **Change Baseline Checks:** Methods that modify material (note duplication, quantizing, loop doubling) measure baselines before dispatching and verify the resulting state one round trip later.
- **Empirical LOM catalog:** Built on a catalog of 1,166 rows across 5 files, with 1,130 verified against a running Ableton Live, spanning the song and transport, tracks, clips, native devices, and the browser.

---

## Quick start

You need Ableton Live 12 and [uv](https://docs.astral.sh/uv/). No repository, no virtual environment, no Python knowledge.

### 1. Install it

```bash
uv tool install git+https://github.com/romanstark/live-maestro.git
```

### 2. Set up Live

```bash
live-maestro-install
```

This finds your Ableton Live User Library, copies the Remote Script into it, and puts the analyzer device where Live's browser looks for it. It prints every path it touched, and it never writes into another script's folder.

### 3. Switch it on in Live

> **Live → Preferences / Settings → Link, Tempo & MIDI → Control Surface → `LiveMaestro`**

Then **quit Live completely and start it again**. Remote Scripts load only at startup: closing the set is not enough, and Live reports nothing either way.

Check that it answers:

```bash
python -m live_maestro.client ping
```

### 4. Connect your AI assistant

Add the server to your MCP client configuration (Claude Desktop, Cursor, Antigravity IDE):

```json
{
  "mcpServers": {
    "live-maestro": {
      "command": "live-maestro"
    }
  }
}
```

To run it without installing anything permanently, let `uvx` fetch it per launch:

```json
{
  "mcpServers": {
    "live-maestro": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/romanstark/live-maestro.git", "live-maestro"]
    }
  }
}
```

Live still needs its two files either way. With this arrangement, install them once with `uvx --from git+https://github.com/romanstark/live-maestro.git live-maestro-install`.

### 5. Put the analyzer device on a track (only for `analyze_audio`)
The installer places `LiveMaestro Analyzer` in your User Library, where Live's browser lists it under **Presets → Audio Effects → Max Audio Effect**. Drag it onto the track you want to measure, **last in the chain**: it reads the signal at its own position, so anything after it is not in what it reports. It passes audio through unchanged.

Only `analyze_audio` uses it. Every other tool works without it, and without it that one tool says so and names these steps. Live does not reload a changed device, so after an update replace any copy already loaded in a set. Skip the device entirely with `live-maestro-install --no-device`.

Three environment variables change where the server looks, and none of them is normally needed:

| Variable | Default | What it changes |
|---|---|---|
| `LIVE_MAESTRO_PORT` | `9878` | The TCP port the Remote Script listens on. Change it in the Remote Script too, or nothing answers. |
| `LIVE_MAESTRO_HOST` | `127.0.0.1` | Where to look for Live. Loopback is the only arrangement this project has measured; the Remote Script speaks no authentication, so anything else is your own arrangement to secure. |
| `LIVE_MAESTRO_ANALYZER_PORT` | `9882` | The UDP port the analyzer device listens on. Change it in the device too. |

Open a project in Ableton Live and start by asking: *"What tracks are in this set?"*

---

## Your music stays yours

- **100% Local:** All communication between your AI assistant and Ableton Live happens over a local, internal loopback connection on your computer.
- **No telemetry or cloud tracking:** Maestro collects zero analytics, has no database, and does not upload your MIDI, audio, project files, or prompts to any external server.
- **Minimal dependencies:** Pure local code with no hidden web scrapers or cloud telemetry.

---

## What stays in your hands

Live's API is extensive, but some things are intentionally reserved for you in the DAW interface:

| Task | Why | How to do it |
|---|---|---|
| **Export / Audio Bounce** | Not exposed by Live's scripting API | Use **File → Export Audio/Video** in Live |
| **Save Project** | Not exposed by Live's API | Press **Ctrl+S** / **Cmd+S** as usual |
| **Group Tracks** | Read-only in Live's API | Press **Ctrl+G** / **Cmd+G** in Live |
| **Track Order** | No LOM method moves a track, so reordering means rebuilding one | Drag the track header in Live |
| **Unconfigured Third-Party VSTs** | How much of a plugin the Object Model sees is decided by the plugin, and Configure mode is how Live adds more | Click **Configure** on the VST and click the parameters you want exposed |
| **Critical Listening** | AI can shape parameters, but only you have ears | Listen on your monitors/headphones and guide the music |

---

## Also using Steinberg Dorico®?

If you also work with music notation, check out **[Dorico Maestro](https://github.com/romanstark/dorico-maestro)**, an MCP server built with the same architecture for Steinberg Dorico. Use the same AI assistant to bridge your workflow between session sketching in Live and engraving parts in Dorico.

---

## Development

Working on the server itself rather than making music with it:

```bash
git clone https://github.com/romanstark/live-maestro.git
cd live-maestro
python -m venv .venv
```

Activate it with `.venv\Scripts\activate` on Windows or `source .venv/bin/activate` on macOS and Linux, then:

```bash
pip install -e ".[dev]"
python scripts/install_script.py
```

The `[dev]` extra adds pytest, ruff and pyright, and is only needed for the tests and the linter. `scripts/install_script.py` is the same installer as `live-maestro-install` and takes the same flags: it calls `live_maestro.installer`, which ships inside the wheel so that a user with no checkout can run it.

Point your MCP client at the checkout's interpreter while you work on it:

```json
{
  "mcpServers": {
    "live-maestro": {
      "command": "/absolute/path/to/live-maestro/.venv/Scripts/python.exe",
      "args": ["-m", "live_maestro.server"]
    }
  }
}
```

On macOS and Linux the interpreter is `.venv/bin/python` rather than `.venv/Scripts/python.exe`.

---

## Documentation & Developer Resources

For technical details, architecture specs, and contributing:

- [docs/architecture.md](docs/architecture.md): Internal architecture and Live Object Model integration
- [docs/protocol.md](docs/protocol.md): Wire protocol and communication specification
- [docs/catalog.md](docs/catalog.md): LOM catalog schema and verification rules
- [docs/limits.md](docs/limits.md): Technical constraints and API boundary measurements
- [docs/upgrading_live.md](docs/upgrading_live.md): Runbook for re-verifying the catalog against a new Live release
- [CONTRIBUTING.md](CONTRIBUTING.md): Contribution guidelines and developer setup
- [THIRD-PARTY.md](THIRD-PARTY.md): Third-party notices, dependencies, and trademark attributions
- [LICENSE](LICENSE): AGPL-3.0 License

---

## Legal & Trademarks

Ableton is a registered trademark, and Live, Max for Live, Link, Drum Rack, Operator and Sampler are trademarks of Ableton AG. Steinberg, Dorico and VST are trademarks or registered trademarks of Steinberg Media Technologies GmbH, registered in Europe and other countries.

Live Maestro is an independent open-source project and is not affiliated with, endorsed, sponsored, or supported by Ableton AG or Steinberg Media Technologies GmbH. For full third-party notices, licenses, and attributions, see [THIRD-PARTY.md](THIRD-PARTY.md).
