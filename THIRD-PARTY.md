# Third-Party Notices

Live Maestro is licensed under the **AGPL-3.0-or-later** license (see [LICENSE](LICENSE)). It interacts with and builds on the third-party components listed below.

---

## Runtime Dependencies

### Model Context Protocol Python SDK (`mcp`)
- **License:** MIT (permissive, compatible with AGPL-3.0)
- **Project:** https://github.com/modelcontextprotocol/python-sdk
- **Usage:** Provides the MCP server foundation (`mcp.server.fastmcp`), communicating over stdio. Installed as a dependency; not vendored.

### PyYAML (`pyyaml`)
- **License:** MIT (permissive, compatible with AGPL-3.0)
- **Project:** https://github.com/yaml/pyyaml
- **Usage:** Loads YAML path catalog files from `src/live_maestro/catalog/*.yaml` at runtime (see [docs/catalog.md](docs/catalog.md)). Installed as a dependency; not vendored.

### Pydantic (`pydantic`)
- **License:** MIT (permissive, compatible with AGPL-3.0)
- **Project:** https://github.com/pydantic/pydantic
- **Usage:** Validates tool argument models in `src/live_maestro/toolargs.py`. Installed as a dependency; not vendored.

---

## Development Dependencies

Development tools include **pytest** (MIT), **ruff** (MIT), **pyright** (MIT) and **pyrefly** (MIT) for testing, linting, and type checking. These are used only in local development and CI environments.

One optional capability reaches outside these: `transcribe_stem` runs **basic-pitch** (Apache-2.0, https://github.com/spotify/basic-pitch) as a subprocess in an environment the user creates. It is neither a dependency nor vendored, and nothing else here needs it.

---

## Ableton Live and Live Object Model

Ableton Live is a commercial product developed by **Ableton AG**. Live Maestro is an independent open-source project and is **not affiliated with, sponsored by, or endorsed by Ableton AG**.

- "Ableton" is a registered trademark, and "Live", "Max for Live", "Link", "Drum Rack", "Operator" and "Sampler" are trademarks of Ableton AG, spelled and capitalised as published in the [Ableton trademark list](https://www.ableton.com/en/legal/trademark-list/). All are used here solely for descriptive purposes to indicate software compatibility.
- The Remote Script imports `_Framework.ControlSurface` and the `Live` module directly from your local Ableton Live installation at runtime. No proprietary Ableton code is redistributed in this repository.
- The Live Object Model definitions in the catalog represent interface descriptions and parameter names observed through runtime introspection.

Ableton Live itself is licensed separately by Ableton AG under their standard software license terms.

---

## Steinberg

Dorico is a commercial product developed by **Steinberg Media Technologies GmbH**, which also owns the VST plug-in format that Live hosts.

- "Steinberg", "Dorico" and "VST" are trademarks or registered trademarks of Steinberg Media Technologies GmbH, registered in Europe and other countries, as published in [Steinberg's imprint](https://www.steinberg.net/legal/imprint/). All are used here solely for descriptive purposes.
- "VST" appears in this documentation only to describe how Live exposes the parameters of third-party plug-ins; Live Maestro implements no part of the format and links against none of its SDK.
- "Dorico" is referenced solely to indicate workflow interoperability with the companion project [Dorico Maestro](https://github.com/romanstark/dorico-maestro).
- No proprietary Steinberg code, binaries, or assets are redistributed in this repository.

