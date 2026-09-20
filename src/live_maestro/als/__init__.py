"""Channel B: the saved project file. Knows nothing about sockets.

A peer of the Live Object Model, not a workaround for it. Channel A sees Live's memory
while Live runs; this package sees the bytes on disk, which are only ever as fresh as
the last save (docs/limits.md section 1).

:mod:`~live_maestro.als.read` is read-only and answers what the LOM cannot be asked
about a project Live does not have open. Over the 174-project corpus (docs/limits.md
section 9), 52 carry clip envelopes and 159 carry track automation, so it reports the
two layers separately and never sums them.

:mod:`~live_maestro.als.write` takes a hash-verified backup before every write with no
opt-out, refuses while the set is open in Live, and re-reads the file afterwards. It
exists for a project Live does not have open, and for track automation, which the LOM
cannot write at all. Neither module opens a socket.
"""

from __future__ import annotations

from live_maestro.als.read import (
    AutomationReport,
    Clip,
    Device,
    DrumPad,
    Envelope,
    EnvelopeRef,
    Macro,
    Note,
    NoteStats,
    Project,
    Sidechain,
    Track,
    automation_of,
    beats_per_bar,
    format_notes_report,
    format_report,
    load_xml,
    note_name,
    read_clip_notes,
    read_project,
    read_track,
    to_dict,
    unique_note_clips,
)
from live_maestro.als.write import (
    AlsRefused,
    AlsWriteError,
    Change,
    LiveCheck,
    SidechainSlot,
    WriteResult,
    list_sidechain_slots,
    restore_backup,
    set_attribute,
    set_sidechain_source,
)

__all__ = [
    "AlsRefused",
    "AlsWriteError",
    "AutomationReport",
    "Change",
    "Clip",
    "Device",
    "DrumPad",
    "Envelope",
    "EnvelopeRef",
    "LiveCheck",
    "Macro",
    "Note",
    "NoteStats",
    "Project",
    "Sidechain",
    "SidechainSlot",
    "Track",
    "WriteResult",
    "automation_of",
    "beats_per_bar",
    "format_notes_report",
    "format_report",
    "list_sidechain_slots",
    "load_xml",
    "note_name",
    "read_clip_notes",
    "read_project",
    "read_track",
    "restore_backup",
    "set_attribute",
    "set_sidechain_source",
    "to_dict",
    "unique_note_clips",
]
