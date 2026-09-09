"""MCP server that drives Ableton Live over the Live Object Model.

The package is layered and the layers do not mix (docs/architecture.md, 'Layers'):

- ``client.py`` knows sockets and no music.
- ``music/`` knows music and no sockets.
- ``lom/`` holds the path grammar and the runtime introspection over it.
- ``als/`` is the second channel: the project file on disk. Reading needs only a saved
  file and does not care whether Live is running. Writing requires the set not to be open in Live.
"""

from __future__ import annotations

__version__ = "0.1.0"
