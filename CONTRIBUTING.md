# Contributing to Live Maestro

Thank you for your interest in contributing to Live Maestro. This project maps Live's Object Model (LOM) as data and provides an MCP server for AI-assisted music production.

Before getting started, please review our core architectural references:
- [docs/architecture.md](docs/architecture.md): System architecture and design goals.
- [docs/protocol.md](docs/protocol.md): Wire protocol specification for socket communication.
- [docs/catalog.md](docs/catalog.md): Catalog schema and path validation rules.
- [docs/limits.md](docs/limits.md): Known API boundaries and constraints.
- [docs/upgrading_live.md](docs/upgrading_live.md): Step-by-step runbook for a new Live version.

---

## 1. Development Setup

### Prerequisites
- Python 3.11 or higher
- Ableton Live 11 or 12
- Git

### Initial Setup

```bash
git clone https://github.com/romanstark/live-maestro.git
cd live-maestro
python -m venv .venv
```

Activate the virtual environment:
- Windows (PowerShell): `.venv\Scripts\activate`
- macOS / Linux: `source .venv/bin/activate`

Install dependencies in editable mode:
```bash
pip install -e ".[dev]"
```

Run the test suite, the linter and both type checkers:
```bash
pytest
ruff check src scripts tests
pyright
pyrefly check
```
CI runs all four. `pyright` reads its paths and its interpreter from `[tool.pyright]` in `pyproject.toml`, so it checks the same thing here as in CI. `pyrefly` enforces the strict rules configured in `[tool.pyrefly.errors]`, among them `unnecessary-type-conversion`, which `pyright` does not flag - so a branch that is clean under `pyright` alone can still be rejected by CI.

The test suite does not require an active Live instance. Continuous Integration (CI) runs tests on Linux, Windows, and macOS without access to a DAW.

### Pointing your MCP client at your working copy

The global `command: "live-maestro"` from the README resolves to whatever is installed on your `PATH`, which is not your local clone.

To exercise the code you are editing, this repository provides pre-configured, project-level MCP configurations using `uv run live-maestro`. When an AI agent launches a project-scoped MCP server, it sets the working directory to the repository root. `uv run` automatically discovers the local `pyproject.toml` and links directly to `src/live_maestro`. Every edit takes effect the next time the server starts—cross-platform across Windows, macOS, and Linux without needing any machine-specific absolute paths.

Pre-configured project files included in the repository:
- **Antigravity / Gemini:** `.agents/mcp_config.json`
- **Cursor:** `.cursor/mcp.json`
- **VS Code / GitHub Copilot:** `.vscode/mcp.json`
- **Claude Code:** `.claude/mcp.json`
- **OpenAI Codex:** `.codex/config.toml`

The configuration uses the standard runner format (JSON):

```json
{
  "mcpServers": {
    "live-maestro": {
      "command": "uv",
      "args": ["run", "live-maestro"]
    }
  }
}
```

And in TOML (`.codex/config.toml`):

```toml
[mcp_servers.live-maestro]
command = "uv"
args = ["run", "live-maestro"]
```

#### Manual Fallback (without uv)

If you develop without `uv` and use a standard `python -m venv`, point your client at Python running the server module directly:

```json
{
  "mcpServers": {
    "live-maestro": {
      "command": "python",
      "args": ["-m", "live_maestro.server"]
    }
  }
}
```
*(On systems where `python` on `PATH` does not point to your virtual environment, replace `"python"` with the path to your virtual environment's Python interpreter, e.g. `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on macOS/Linux).*

---

## 2. Core Workflow: Verifying Catalog Rows

The catalog in `src/live_maestro/catalog/*.yaml` describes the fixed Live Object Model surface. Each entry represents an addressable path in Live.

Each catalog row has a `status` field, which defaults to `untested`:
- `verified`: Confirmed working against an active Live instance.
- `broken`: Exists on the object, but Live refuses the call or raises an error.
- `untested`: Defined in the catalog but not yet validated live.

Verifying catalog entries against running Live installations is one of the most helpful contributions you can make.

### Available Helper Scripts

Run repository helper scripts with the selected environment's Python, for example
`python scripts/probe_paths.py`. They do not require executable file permissions,
so the same invocation works with source distributions built on Windows.

| Script | Purpose |
|---|---|
| `scripts/probe_paths.py` | Tests specific catalog rows against an open Live set and updates their status. |
| `scripts/sync_catalog.py` | Compares Live's runtime objects against the catalog to find missing or obsolete paths. |
| `scripts/install_script.py` | Copies the Remote Script into the User Library, clears `__pycache__`, and reports what discovery found with `--list`. A wrapper around `live_maestro.installer`, which is the same code the installed `live-maestro-install` runs, so a fix reaches both. |
| `scripts/build_analyzer_device.py` | Generates the Channel C Max for Live device from the protocol contract. `--check` fails when the committed files no longer match. |
| `scripts/install_script.py` | Also installs the generated device. `--no-device` skips it, `--device-source` points at another file. |
| `scripts/inventory.py` | Surveys the open set and the local install: Live versions, User Library, script presence, log diagnostics, listening port. `--installation-only` runs without Live. |

### How to Probe a Path

1. **Open a test set in Live.** Use a disposable project rather than an active production set.
2. Ensure the Remote Script is installed and selected in Live preferences (`python scripts/install_script.py`).
3. Run a dry run to inspect the target paths:
   ```bash
   python scripts/probe_paths.py --id clip.warping --track 0 --slot 0
   ```
4. Execute the live probe:
   ```bash
   python scripts/probe_paths.py --id clip.warping --track 0 --slot 0 --go
   ```
5. Update the row in `src/live_maestro/catalog/`:
   - If read and write succeed: set `status: verified` and document what was tested in `doc`.
   - If Live returns an error (`live_error`): check whether the test value was within valid range. If within range and refused by Live, set `status: broken` with the error explanation.
   - If the property does not exist (`no_such_path`): remove the row from the catalog and document the finding in [docs/limits.md](docs/limits.md).
   - If a target was missing (`index_out_of_range`): keep `status: untested` until tested with an appropriate set.

A row id is also a lookup anchor. `src/live_maestro/vocabulary.py` points producer words at row ids, and `tests/test_lookup.py` fails when an anchor names a row that no longer exists or one whose status is no longer `verified`. Removing a row, renaming it, or flipping it to `broken` means re-pointing its anchors in the same change.

### Updating limits and catalog verification

A limit in [docs/limits.md](docs/limits.md) represents an empirical measurement. If a newer Live version or different configuration lifts a limitation:
- Test the behavior against an active Live instance, noting the exact Live version and platform.
- Update the relevant entry in [docs/limits.md](docs/limits.md) with the new measurement, rather than simply deleting past evidence, so developers understand when and how the limitation changed.

In the catalog (`src/live_maestro/catalog/*.yaml`), empirical verification is recorded in the `proven` field (e.g. `proven: [get, set]`), which `scripts/probe_paths.py` updates automatically upon running successful probes. Specific behavioral findings (such as parameter ranges, units, or discrete step sizes) are documented in the row's `doc` field.

Corpus figures in the documentation describe aggregate statistics from project collections ([docs/limits.md section 9](docs/limits.md)). When updating or adding constraints, prefer direct measurements from a running Live session.

---

## 3. The Channel C Device

`analyze_audio` reads `devices/LiveMaestro_Analyzer.amxd`, a Max for Live audio effect. The device and the client both take the port, the reply field order, the band edges and the schema number from `src/live_maestro/analyzer/protocol.py`. The device is generated from that file. Do not edit it by hand.

### Rebuilding it

```bash
python scripts/build_analyzer_device.py
```

This writes `LiveMaestro_Analyzer.maxpat`, which `git diff` can show, and `LiveMaestro_Analyzer.amxd`, which Live loads. Commit both. `--check` verifies that the files on disk match the generator and writes nothing, and `tests/test_analyzer_device.py` runs it, so a protocol change without a rebuild is a red build.

Any change to the reply layout raises `REPLY_SCHEMA` in the same commit. A device answering with a schema the client does not read is refused with `schema_mismatch` rather than misread against the wrong layout.

### Adding an object to the patcher

`SHAPES` in `src/live_maestro/maxgen.py` lists every Max object a generated device may use, with its inlet and outlet counts. It sits in the package rather than in `scripts/build_analyzer_device.py` because a build script is not importable and every generator, and every test, needs the same table. Add an entry only after looking the object up in the reference pages that ship with Live:

```
<Live install>/Resources/Max/resources/docs/refpages/
```

Max draws an object name it does not have as a broken box, reports no error and measures nothing, so an unverified name fails silently. `tests/test_devices.py` refuses a patcher holding an object that is not in `SHAPES`, and refuses one where a measurement object's outlet is wired nowhere.

### Testing it against Live

1. Install it with `python scripts/install_script.py`, which resolves the User Library and copies the device into `Presets/Audio Effects/Max Audio Effect/`, where Live's browser looks. Copying by hand works too, but the User Library is not always under `Documents` and a device in the wrong folder simply never appears, with no error from Live.
2. Put it on the track to measure. Max does not reload a changed `.amxd`, so replacing the file means deleting the device in Live and adding it again.
3. Restart the MCP client after changing `protocol.py`. The running server holds the old contract until it is restarted.
4. Call `analyze_audio` with `track="*"` to read whichever instance answers, without depending on track matching. A name and `master` match without a round trip; an index costs one, because the device reports the name of its track and not its position.

A measurement needs audio through the device. A set with no instruments measures as `silent`, which is correct; Live's own `song.master_track.output_meter_level` settles whether audio is flowing at all.

Findings about the Max objects themselves go in [docs/limits.md](docs/limits.md) with the date, the Live version and the numbers.

---

## 4. Pull Request Guidelines

1. Fork the repository and create a feature branch from `main`.
2. Ensure all tests pass (`pytest`), linting is clean (`ruff check src scripts tests`) and both type checks are clean (`pyright`, `pyrefly check`). CI runs all four.
3. In your pull request description, specify:
   - What changed and why.
   - The OS and Live version used for testing.
   - Any new or updated catalog paths.

---

## 5. Coding Conventions

- **Language:** English for all code, comments, docstrings, and documentation.
- **Python Code Style:** Python 3.11+, complete type annotations, 100 character line length, compliant with `ruff`.
- **Remote Script Restrictions:** `live-remote-script/` runs directly inside Live's Python interpreter. It must use only the standard library, avoid external dependencies, and avoid syntax unsupported by older Python interpreters.
- **Separation of Layers:**
  - `client.py`: Socket transport and wire serialisation.
  - `music/`: Pure Python musical theory, notes, and humanisation without socket dependencies.
  - `executor.py`: Connects catalog specifications to client dispatch.
  - `server.py`: MCP tool definitions and high-level endpoints.
  - `lookup.py` / `vocabulary.py`: Turns an intent in the caller's own words into a route and ranked catalog rows. Reads the catalog only, never the socket.
  - `analyzer/`: Channel C. The socket lives in `client.py` alone; every derived figure and every verdict is computed in `metrics.py` and `report.py`, which is what makes them testable without a DAW.
  - `audio/`: offline sample-file inspection. Standard library only, no Live and no external codec, so it runs anywhere the tests do.
  - `maxgen.py`: the parts every generated Max for Live device shares, including `SHAPES`, the patcher writer, the `.amxd` container and the `--out`/`--check` command line. A generator under `scripts/` supplies only its own graph.
- **Remote Script Changes:** Adding handlers inside Live requires a full restart of Live. Prefer expressing capabilities as catalog rows whenever possible.
- **Safety Checks:** Method calls must be explicitly listed in the Remote Script's allowlist. Destructive operations (such as deleting tracks or devices) require `confirm=True`.

---

## 6. Contributor License Agreement (CLA)

Live Maestro is dual-licensed (AGPL-3.0 for open source, with separate commercial licensing available). Contributors agree to a lightweight [CLA](CLA.md) before their first pull request is merged, ensuring you retain copyright while allowing dual-licensing maintenance.

### Third-Party Code

Please submit original work. If a patch incorporates code from other open-source projects, clearly identify the source and its license in the pull request description so appropriate notices can be preserved.

---

## 7. Commercial Inquiries

For commercial licensing options (e.g., proprietary embedding without AGPL requirements), please contact Roman Stark (mail@romanstark.de).
