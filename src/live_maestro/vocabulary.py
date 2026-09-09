"""Studio vocabulary: what a producer says, and which mechanism answers it.

Three route tables and one synonym table, all read by :mod:`live_maestro.lookup`.

Why this is Python and not YAML, when the catalog next door is YAML: a catalog row
describes a place in Live and is verified against a running Live, so it earns a schema, a
loader and a status lifecycle. These tables describe English, they are verified against
this repository rather than against Live, and every entry is dead weight the moment the
thing it points at moves. Keeping them as code means ruff, pyright and the test suite all
see them, and a renamed row breaks a test instead of silently degrading a search.

Each route table answers an intent that catalog rows cannot answer, and answering it
badly is worse than not answering: a plausible wrong row reads like a result. Measured
over 444 producer queries, the substring search these replace put ``track.mute`` first
for "sidechain" and returned nothing at all for "cutoff".
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "BLOCKED",
    "DEVICE_PARAMETER",
    "INTENT_TOOL",
    "SYNONYMS",
    "RouteEntry",
    "all_anchor_ids",
]


@dataclass(frozen=True)
class RouteEntry:
    """One intent that is answered by something other than a ranked catalog row.

    Attributes:
        terms: Trigger words. A single word matches a query term or its stem. A phrase
            with a space in it has to occur in the query as written.
        answer: The sentence handed to the caller. It says what is true, not what failed.
        evidence: Where in this repository the claim is established.
        next_call: The tool or path to use instead, named exactly.
    """

    terms: tuple[str, ...]
    answer: str
    evidence: str = ""
    next_call: str = ""


@dataclass(frozen=True)
class Synonym:
    """A bridge from studio words to words that occur in catalog rows.

    Attributes:
        producer: What a producer types.
        catalog: Words that genuinely occur in the target rows and can be scored.
        anchors: Row ids this vocabulary should reach, best first. Tested to exist.
    """

    producer: tuple[str, ...]
    catalog: tuple[str, ...]
    anchors: tuple[str, ...] = field(default_factory=tuple)


#: Intents Live's API cannot perform at all. A confident no with the manual alternative
#: is a better answer than five rows that look nearly right (docs/limits.md).
BLOCKED: tuple[RouteEntry, ...] = (
    RouteEntry(
        terms=(
            "automate the master", "master fader automation", "automate the main",
            "automate the main fader", "automate a return", "automate the return",
            "arrangement automation", "automation in the arrangement",
            "automate over the arrangement", "automation lane", "automation lanes",
            # Not "master fade": the phrase matcher tolerates words in between, so
            # it fires on "master fader level", which is an ordinary question about
            # the level and not about automating it.
            "fade out the master", "fade the master out", "fade the whole mix",
        ),
        answer=(
            "Automation over this channel is a clip envelope, and an envelope lives in a "
            "Session clip on the same track as the parameter it drives. The main track and "
            "the return tracks have no clip slots at all, so no envelope can exist for "
            "their parameters: writing one answers 'Not a session clip or parameter belongs "
            "to another track'. Arrangement track automation is a second, separate thing "
            "and is not writable here either (docs/limits.md, section 2, 'Track automation "
            "vs. clip envelopes (LOM)'). What does work: write_automation into a track's "
            "own Session clip, then arrange to copy that clip to the timeline, which "
            "carries the envelope with it. A whole-mix fade therefore has to be automated "
            "per track, or drawn in Live by hand. For a static main level that is not "
            "automation at all, write master.volume_value with lom_set."
        ),
        evidence=(
            "docs/limits.md, section 2, 'Track automation vs. clip envelopes (LOM)'; "
            "catalog row master.duplicate_clip_to_arrangement records clip_slots count 0 "
            "on the main track, measured 2026-08-30 against Live 12.4.5"
        ),
        next_call="write_automation into the track's own Session clip, then arrange",
    ),
    RouteEntry(
        terms=(
            "export", "bounce", "render", "mixdown", "bounced", "exporting", "bounce down",
            "render out", "print the track",
        ),
        answer=(
            "Nothing here can produce an audio file: Live's Object Model exposes no render, bounce "
            "or export function at all, so no path, tool or catalog row reaches it "
            "(docs/limits.md, section 1, 'Audio rendering and exporting (LOM)'). Do it in Live: "
            "set the range with the Arrangement loop brace, then File -> Export Audio/Video "
            "(Ctrl+Shift+R on Windows, Cmd+Shift+R on macOS); MIDI leaves the same way, via File "
            "-> Export MIDI Clip. Bringing a file that already exists into the set is the other "
            "direction and is not blocked: lom_call on a clip slot, method create_audio_clip, one "
            "absolute path (catalog row clip_slot.create_audio_clip)."
        ),
        evidence=(
            "docs/limits.md, section 1, 'Audio rendering and exporting (LOM)'; no "
            "export/render/bounce row exists in src/live_maestro/catalog/*.yaml"
        ),
        next_call="set_loop to fix the render range, then export by hand in Live",
    ),
    RouteEntry(
        terms=(
            "freeze", "flatten", "consolidate", "freeze the track", "bounce in place",
            "render in place", "commit the track",
        ),
        answer=(
            "Freeze, Flatten and Consolidate are all rendering operations, and rendering is the "
            "one thing the Object Model exposes no function for, so there is no method and no "
            "catalog row for any of them (docs/limits.md, section 1, 'Audio rendering and "
            "exporting (LOM)'). Do it in Live: right-click the track header for Freeze Track and "
            "then Flatten, or select a range in the Arrangement and press Ctrl+J (Cmd+J) to "
            "consolidate. Maestro can rearrange and re-parameterise what exists, but it cannot "
            "create new audio material."
        ),
        evidence=(
            "docs/limits.md, section 1, 'Audio rendering and exporting (LOM)'; "
            "src/live_maestro/catalog/*.yaml contains no freeze, flatten or consolidate row"
        ),
    ),
    RouteEntry(
        terms=(
            "save the set", "save the project", "save my project", "save the song", "save this",
            "save it", "save my work", "save changes", "unsaved", "ctrl s", "cmd s",
            "save first",
        ),
        answer=(
            "The LOM provides no save method, so nothing here can save the set and every change "
            "Maestro makes lives only in Live's memory until you press Ctrl+S (Cmd+S) in Live "
            "yourself (docs/limits.md, section 1, 'Saving the project (LOM)'). A .als on disk is "
            "the state of the last manual save, which is why als_read looks stale after a live "
            "edit and why als_write requires the set not to be open in Live."
        ),
        evidence=(
            "docs/limits.md, section 1, 'Saving the project (LOM)'; "
            "src/live_maestro/als/write.py, _guard"
        ),
        next_call="Ctrl+S in Live, then als_read to inspect the saved file",
    ),
    RouteEntry(
        terms=(
            "group the", "group these", "group them", "group my", "group tracks",
            "into a group", "make a group", "group track for", "bus these", "sub group",
            "subgroup",
        ),
        answer=(
            "Tracks cannot be grouped from here: group_track and is_grouped are read-only "
            "properties in the LOM, so there is no create-group call and no way to move a track "
            "into or out of one (docs/limits.md, section 2, 'Track grouping (LOM)'). Select the "
            "tracks in Live and press Ctrl+G (Cmd+G). Grouping inserts a container track and "
            "shifts the index of every track after it, so re-run get_session afterwards rather "
            "than reusing cached track numbers."
        ),
        evidence=(
            "docs/limits.md, section 2, 'Track grouping (LOM)'; row track.group_track in "
            "src/live_maestro/catalog/20-track.yaml is access [get, observe]"
        ),
        next_call=(
            "get_session after grouping in Live; track.is_grouped and track.is_foldable read what "
            "already exists"
        ),
    ),
    RouteEntry(
        terms=(
            "reorder the tracks", "reorder tracks", "move the track", "move track",
            "move this track", "move that track", "rearrange the tracks", "track order",
            "order of the tracks", "drag the track", "put the track above",
            "put the track below",
        ),
        answer=(
            "A track's position is fixed once it is created: neither Track nor Song offers a "
            "method for moving one, so there is no reordering call (docs/limits.md, section 2, "
            "'Track reordering (LOM)'). Drag the track header in Live. Doing it from here means "
            "creating a track at the target index, loading the instrument again, rebuilding clips, "
            "notes and mixer settings and deleting the original, and anything not explicitly "
            "recreated is lost while every cached index shifts. Devices are the exception: "
            "song.move_device does reorder those."
        ),
        evidence=(
            "docs/limits.md, section 2, 'Track reordering (LOM)'; rows song.create_audio_track, "
            "song.delete_track and song.move_device in src/live_maestro/catalog/10-song.yaml"
        ),
        next_call=(
            "song.move_device for devices; track reordering has to be done by dragging in Live"
        ),
    ),
    RouteEntry(
        terms=(
            "track height", "taller", "shorter lane", "resize the track", "bigger lane",
            "track lane size", "expand the track lane",
        ),
        answer=(
            "Track height is a view property Live keeps out of the Object Model: no catalog row "
            "reaches it and no path answers it, which is the same class of gap as the properties "
            "listed in docs/limits.md, section 7. Resize it in Live by dragging the lower border "
            "of the track's header in the Arrangement, or press Alt+drag to size them all. The one "
            "related thing that is writable from here is track.fold_state, which folds a group "
            "track open or shut, not the same as height."
        ),
        evidence=(
            "docs/limits.md, section 7, 'Properties that do not exist, and are therefore not in "
            "the catalog'; no height row in src/live_maestro/catalog/*.yaml; row "
            "track.fold_state in 20-track.yaml"
        ),
        next_call="track.fold_state to fold a group track open or shut",
    ),
    RouteEntry(
        terms=(
            "track delay", "delay the whole track", "nudge the track", "delay compensation",
            "track offset", "milliseconds early", "milliseconds late",
        ),
        answer=(
            "Track Delay is not in the Object Model and has no catalog row, so it cannot be read "
            "or written from here: the same gap docs/limits.md, section 7 describes for "
            "properties Live does not expose. Set it in Live: View -> Mixer, then type into the "
            "Track Delay lane on the mixer strip. To shift material from here instead, move the "
            "notes with write_clip_notes or move the clip's start_marker, which changes what plays "
            "rather than when the track plays."
        ),
        evidence=(
            "docs/limits.md, section 7, 'Properties that do not exist, and are therefore not in "
            "the catalog'; no track delay row in src/live_maestro/catalog/*.yaml; row "
            "clip.start_marker in 30-clip.yaml"
        ),
        next_call="write_clip_notes with shifted start times, or clip.start_marker",
    ),
    RouteEntry(
        terms=(
            "phase invert", "invert the phase", "flip the phase", "phase flip", "polarity",
            "out of phase", "phase button",
        ),
        answer=(
            "Live has no per-track phase or polarity switch, so there is nothing for the LOM to "
            "expose and no catalog row for it: the same kind of gap docs/limits.md, section 7 "
            "records. Insert a Utility on the track and use its Phase L and Phase R invert "
            "switches; those are ordinary device parameters, so load the device, find the index "
            "with describe(with_parameters=True) and write it with set_parameter."
        ),
        evidence=(
            "docs/limits.md, section 7, 'Properties that do not exist, and are therefore not in "
            "the catalog'; no phase or invert row in src/live_maestro/catalog/*.yaml"
        ),
        next_call=(
            "load_device('Utility'), then describe(path, with_parameters=True), then set_parameter"
        ),
    ),
    RouteEntry(
        terms=(
            "follow action", "follow actions", "clip follows", "auto advance the clip",
            "jump to the next clip automatically",
        ),
        answer=(
            "Follow actions are managed by Live's GUI and are not exposed in the LOM: "
            "clip.follow_action_* returns no_such_path, so nothing here reads or writes them "
            "(docs/limits.md, section 7). Set them in Live's Clip view Launch panel. From here the "
            "equivalent is to make the sequence explicit: fire clips yourself with clip.fire or "
            "clip_slot.fire, or lay the order out on the timeline with arrange."
        ),
        evidence=(
            "docs/limits.md, section 7 (clip.follow_action_*); rows clip.fire and clip_slot.fire "
            "in src/live_maestro/catalog/30-clip.yaml"
        ),
        next_call=(
            "arrange to place the sequence on the timeline, or clip_slot.fire to trigger "
            "explicitly"
        ),
    ),
    RouteEntry(
        terms=(
            "add a warp marker", "add warp marker", "new warp marker", "create a warp marker",
            "insert a warp marker", "place a warp marker", "set a warp marker", "pin the beat",
        ),
        answer=(
            "Warp markers cannot be created from here. Clip.add_warp_marker takes a WarpMarker "
            "object that has to be constructed inside Live, and Live answers type_error, 'No "
            "registered converter was able to produce a C++ rvalue'; the {\"__path__\": ...} form "
            "resolves an existing object and cannot build a new one (docs/limits.md, section 5, "
            "'Warp markers'). Double-click the waveform in Live's Clip view to add one. Reading, "
            "moving and mostly removing markers do work from here."
        ),
        evidence=(
            "docs/limits.md, section 5, 'Warp markers'; row clip.add_warp_marker in "
            "src/live_maestro/catalog/30-clip.yaml is status: broken"
        ),
        next_call=(
            "clip.move_warp_marker and clip.warp_marker_beat_time for markers that already exist"
        ),
    ),
    RouteEntry(
        terms=(
            "shadow marker", "remove the last warp marker", "delete the last warp marker",
            "delete a warp marker", "remove a warp marker",
        ),
        answer=(
            "Removing warp markers only half works and its success result lies. Live's highest "
            "marker is a shadow marker and refuses deletion outright, and aimed at the last real "
            "marker remove_warp_marker reports success and removes nothing, measured 2026-09-05 "
            "against Live 12.4.5, the count went 3 to 2 on the first removal and 2 to 2 on the "
            "second, both reported as success (docs/limits.md, section 5, 'Warp markers'). Never "
            "read that success as a removal; compare clip.warp_marker_beat_time per index before "
            "and after, which is why the warp_state hook checks the count. To undo warping "
            "entirely, switch clip.warping off rather than deleting markers, or redo the warp by "
            "hand in Live's Clip view."
        ),
        evidence=(
            "docs/limits.md, section 5, 'Warp markers'; rows clip.remove_warp_marker and "
            "clip.warping in src/live_maestro/catalog/30-clip.yaml"
        ),
        next_call=(
            "clip.warping to unwarp; clip.warp_marker_beat_time before and after to prove a count "
            "changed"
        ),
    ),
    RouteEntry(
        terms=(
            "warp the midi", "warp a midi clip", "warp markers on midi", "midi warp",
            "stretch the midi clip",
        ),
        answer=(
            "Warping is an audio-clip facility and does not exist on MIDI clips: measured "
            "2026-09-05 against Live 12.4.5, reading clip.warp_markers on a MIDI clip answers "
            "live_error, 'Warp markers are only available for Audio Clips', and remove_warp_marker "
            "answers 'Warping is only available for audio clips' (docs/limits.md, section 8, "
            "'Preconditions are part of the question'). To change MIDI timing use quantize_clip, "
            "or move the note start times with write_clip_notes; to time-stretch the part, change "
            "the tempo or the clip's loop brace in Live."
        ),
        evidence=(
            "docs/limits.md, section 8, 'Preconditions are part of the question'; rows "
            "clip.warp_markers and clip.warping in src/live_maestro/catalog/30-clip.yaml"
        ),
        next_call="quantize_clip, or write_clip_notes with new start times",
    ),
    RouteEntry(
        terms=(
            "delete the track automation", "remove the track automation",
            "clear the automation lane", "delete the automation", "remove the automation",
            "get rid of the automation", "erase the automation",
        ),
        answer=(
            "Only the clip envelope can be cleared from here; the Arrangement track automation "
            "lane underneath cannot be read or deleted at all (docs/limits.md, section 2, 'Track "
            "automation vs. clip envelopes'). Where both target the same parameter over the same "
            "stretch the clip envelope is the one that plays, so removing it does not tidy a "
            "redundant curve: it uncovers the track automation below. On a measured 51-track "
            "set that meant a filter jumping from around 200 Hz to 10 kHz. Delete the lane "
            "in Live by selecting the range in the automation lane and "
            "pressing Delete; clear_automation here removes the clip envelope only, and does so "
            "knowingly."
        ),
        evidence=(
            "docs/limits.md, section 2, 'Track automation vs. clip envelopes' (measured "
            "2026-09-01, Live 12.4.5)"
        ),
        next_call=(
            "clear_automation removes the clip envelope; the track lane has to be edited in Live"
        ),
    ),
    RouteEntry(
        terms=(
            "track automation", "automation lane", "automation lanes", "arrangement automation",
            "automate the arrangement", "automate in the arrangement", "draw automation",
            "automation on the timeline", "breakpoint in the arrangement",
        ),
        answer=(
            "Arrangement track automation is not writable from here: clip.automation_envelope() "
            "returns None for Arrangement clips, always, and create_automation_envelope is refused "
            "there with 'Not a session clip or parameter belongs to another track' "
            "(docs/limits.md, section 2, 'Track automation vs. clip envelopes'). The working order "
            "is fixed: write the curve into a Session clip with write_automation, then copy that "
            "clip to the timeline with arrange, and the envelope travels with the copy. In the "
            "other order the verification passes and every Arrangement clip is empty, because "
            "automation_read reads the Session clip. Drawing directly into a lane has to happen in "
            "Live."
        ),
        evidence=(
            "docs/limits.md, section 2, 'Track automation vs. clip envelopes'; rows "
            "arrangement_clip.automation_envelope (verified: returns None) and "
            "arrangement_clip.create_automation_envelope (status: broken) in "
            "src/live_maestro/catalog/30-clip.yaml"
        ),
        next_call="write_automation on the Session clip, then arrange to place it",
    ),
    RouteEntry(
        terms=(
            "move the clip", "move the clips", "move this clip", "slide the clip",
            "drag the clip", "reposition the clip", "move the arrangement clip",
            "later in the arrangement", "earlier in the arrangement", "move it to bar",
        ),
        answer=(
            "An Arrangement clip cannot be moved once it exists: start_time and end_time are "
            "read-only and nothing else in the catalog slides a clip along the timeline: "
            "arrangement_clip.position moves the loop brace inside the clip, not the clip along "
            "the song, and writing it expecting the clip to move will silently change what part of "
            "the material plays. Position is fixed at duplication time. Drag it in Live, or delete "
            "it with arrangement_clip.delete and place a fresh copy with arrange at the beat you "
            "want. In the Session view a clip is copied to another slot with "
            "clip_slot.duplicate_clip_to, which is a copy, not a move."
        ),
        evidence=(
            "rows arrangement_clip.start_time (access [get, observe]), arrangement_clip.position "
            "and clip_slot.duplicate_clip_to in src/live_maestro/catalog/30-clip.yaml"
        ),
        next_call="arrangement_clip.delete then arrange(at_beat=...)",
    ),
    RouteEntry(
        terms=(
            "arm the master", "arm the main", "arm the return", "arm a return",
            "record arm the master", "monitor the master", "monitor the return",
            "input on the master",
        ),
        answer=(
            "Main and Return tracks do not support arming or monitoring: master.arm and return.arm "
            "return no_such_path, and master.can_be_armed and return.can_be_armed read false by "
            "design (docs/limits.md, section 7). Record onto a normal audio track instead (set "
            "track.arm and pick track.current_monitoring_state there), and route the Main or the "
            "return into it in Live if capturing that bus is what you meant."
        ),
        evidence=(
            "docs/limits.md, section 7 (master.arm, return.arm); rows master.can_be_armed and "
            "return.can_be_armed in src/live_maestro/catalog/20-track.yaml"
        ),
        next_call=(
            "track.can_be_armed to check, then track.arm and track.current_monitoring_state on an "
            "audio track"
        ),
    ),
    RouteEntry(
        terms=(
            "mute the master", "mute the main", "solo the master", "solo the main",
            "master mute", "kill the master",
        ),
        answer=(
            "The Main track has no mute or solo button in Live, and the LOM raises on access "
            "rather than quietly reading false: master.mute is a broken row for exactly that "
            "reason, measured 2026-08-29 against Live 12.4.5. Silence the Main with its activator "
            "instead: write master.track_activator, which is the Main strip's on/off switch, or "
            "mute the individual tracks."
        ),
        evidence=(
            "rows master.mute (status: broken) and master.track_activator in "
            "src/live_maestro/catalog/20-track.yaml"
        ),
        next_call="master.track_activator",
    ),
    RouteEntry(
        terms=(
            "preset", "presets", "load a preset", "save a preset", "factory preset",
            "device preset", "recall a preset",
        ),
        answer=(
            "device.presets does not exist: presets are not on the Device object at all "
            "(docs/limits.md, section 7). They come from the Browser: use load_device with the "
            "preset's name, or walk app.browser and call browser.load_item after selecting the "
            "target track, remembering that an effect is appended to the chain while an instrument "
            "replaces the one already there. Saving a preset back is a drag-to-browser action in "
            "Live with no LOM route."
        ),
        evidence=(
            "docs/limits.md, section 7 (device.presets); row browser.load_item in "
            "src/live_maestro/catalog/50-browser.yaml"
        ),
        next_call="load_device, or view.selected_track then browser.load_item",
    ),
    RouteEntry(
        terms=(
            "sampler sample", "sampler playback", "sampler zone", "sampler zones",
            "multisampler", "in sampler", "on the sampler", "sampler key range",
        ),
        answer=(
            "Sampler (MultiSampler) exposes only the common Device interface: sampler.sample and "
            "sampler.playback_mode return no_such_path, and the zone editor is not in the LOM at "
            "all (docs/limits.md, section 7). Edit zones and sample slots in Live's Sampler GUI. "
            "From here, its exposed controls are ordinary parameters: walk device.parameters and "
            "match names, which is what sampler.parameter_by_name records as the honest ceiling. "
            "Simpler is the device that does carry sample properties: simpler.sample, "
            "simpler.playback_mode and the simpler.sample_* rows."
        ),
        evidence=(
            "docs/limits.md, section 7 (sampler.sample, sampler.playback_mode); row "
            "sampler.parameter_by_name in src/live_maestro/catalog/40-device.yaml; rows "
            "simpler.sample and simpler.playback_mode"
        ),
        next_call=(
            "describe(path, with_parameters=True) then set_parameter; simpler.* rows if the device "
            "is Simpler"
        ),
    ),
    RouteEntry(
        terms=(
            "only one parameter", "no parameters", "vst parameter", "plugin parameter",
            "parameter strip", "configure mode", "plugin exposes", "cannot see the plugin",
            "parameter count",
        ),
        answer=(
            "How much of a plugin the Object Model sees is decided by the plugin, not by you: "
            "measured 2026-09-01 against Live 12.4.5, two VST3 instruments loaded with a single "
            "parameter each while a VST2 effect loaded with 17, and neither format, vendor nor "
            "instrument-versus-effect predicts it (docs/limits.md, section 3, 'Third-party VST "
            "parameter exposure (Live)'). Read parameter_count from describe rather than guessing, "
            "and device.get_parameter_names for what the plugin declares. To put a control on the "
            "strip, press Configure in Live, click the control in the plugin's own GUI and leave "
            "the mode; Live allocates 128 slots per instance and stores the mapping in the set, so "
            "a second instance starts empty. With the set closed in Live, als_write operation "
            "'configure' fills those slots in the file instead."
        ),
        evidence=(
            "docs/limits.md, section 3, 'Third-party VST parameter exposure (Live)'; row "
            "device.get_parameter_names in src/live_maestro/catalog/40-device.yaml; "
            "src/live_maestro/als/write.py, configure_plugin_parameters"
        ),
        next_call=(
            "describe(path, with_parameters=True), then device.get_parameter_names; "
            "als_write(operation='configure') with the set closed in Live"
        ),
    ),
    RouteEntry(
        terms=(
            "vst2", "display units", "no units", "normalized instead of hz",
            "normalised instead of hz", "why is it 0 to 1",
        ),
        answer=(
            "str_for_value() returns no formatted physical units for VST2 plugins: the value "
            "stays a normalized float between 0.0 and 1.0 and there is no route to the plugin's "
            "own display string (docs/limits.md, section 3, 'VST2 display units (Live)'). Read the "
            "number off the plugin's own GUI in Live to map it. From here, set the internal value "
            "and confirm the read-back rather than expecting Hz or dB; on Live's own devices the "
            "display field does carry units, and on a Max device the internal range is whatever "
            "the device defines, not hertz."
        ),
        evidence=(
            "docs/limits.md, section 3, 'VST2 display units (Live)' and 'Normalized value vs. "
            "physical display value (Live)'; rows param.value and param.str_for_value in "
            "src/live_maestro/catalog/40-device.yaml"
        ),
        next_call=(
            "param.value with param.str_for_value; the display field stays empty for VST2"
        ),
    ),
    RouteEntry(
        terms=(
            "rollback", "roll back", "atomic", "transaction", "transactional", "undo the batch",
            "revert the batch", "all or nothing",
        ),
        answer=(
            "lom_batch is not transactional: the LOM supports no database-style rollback, and "
            "atomic: true only stops execution at the first error, and every step already executed "
            "stays in effect (docs/limits.md, section 4, 'Non-transactional batch operations'). "
            "Live's own undo is the recovery route: wrap the pass with song.begin_undo_step and "
            "song.end_undo_step so it collapses into one step, then song.undo, or Ctrl+Z in Live. "
            "Otherwise read the state back and repair it explicitly."
        ),
        evidence=(
            "docs/limits.md, section 4, 'Non-transactional batch operations'; rows song.undo, "
            "song.begin_undo_step and song.end_undo_step in "
            "src/live_maestro/catalog/10-song.yaml"
        ),
        next_call="song.begin_undo_step before the batch, song.undo to unwind",
    ),
    RouteEntry(
        terms=(
            "retry", "retries", "timed out", "timeout", "try it again", "send it again",
            "resend",
        ),
        answer=(
            "A write that timed out is never retried automatically, and you should not retry it by "
            "hand either: the write may already have succeeded inside Live, and repeating it "
            "duplicates the action: duplicate notes, a second arrangement clip (docs/limits.md, "
            "section 4, 'Write timeouts and retry policy'). Read the state back first with "
            "lom_get, get_clip or get_track and only then decide. One round trip costs about 450 "
            "ms, measured 2026-08-29 against Live 12.4.5, so put the whole pass into a single "
            "lom_batch rather than retrying piecemeal."
        ),
        evidence=(
            "docs/limits.md, section 4, 'Write timeouts and retry policy' and 'Round-trip cost'"
        ),
        next_call="lom_get or get_clip to see what actually landed, then act on that",
    ),
    RouteEntry(
        terms=(
            "in parallel", "concurrently", "multiplex", "second connection",
            "parallel requests", "simultaneous requests", "two at once",
        ),
        answer=(
            "The socket processes one request at a time per connection with no multiplexing, so "
            "nothing here can run in parallel (docs/limits.md, section 4, 'Strictly serial "
            "transport'). Put the whole pass into one lom_batch instead: a twenty-track mixer "
            "sweep is twenty round trips without it and one with it, at roughly 450 ms per trip "
            "measured 2026-08-29 against Live 12.4.5. Note that batch steps still execute "
            "sequentially and cannot be rolled back."
        ),
        evidence="docs/limits.md, section 4, 'Strictly serial transport' and 'Round-trip cost'",
        next_call="lom_batch",
    ),
    RouteEntry(
        terms=(
            "sound good", "sounds good", "does it sound", "how does it sound", "sounds right",
            "sounds better", "in tune", "is the mix", "mix balanced", "sounds muddy",
            "judge the",
        ),
        answer=(
            "Maestro reads stored numbers, never audio: it can confirm a cutoff is at 800 Hz but "
            "cannot evaluate musical quality, vocal tuning or balance, and there is no audio "
            "return path of any kind (docs/limits.md, section 1, 'Auditory judgment (Physical)'). "
            "Listen in Live and judge by ear: set_arrangement_time to put the transport where you "
            "want to hear it, then play. Ask instead for the values, and the judgment stays yours."
        ),
        evidence="docs/limits.md, section 1, 'Auditory judgment (Physical)', and section 6",
        next_call="set_arrangement_time then play, and listen in Live",
    ),
    RouteEntry(
        terms=(
            "can you hear", "is it audible", "hear the difference", "did it actually change",
            "prove it worked", "did anything happen", "take effect",
        ),
        answer=(
            "A success proves the Remote Script executed the command and, for lom_set, that the "
            "value is stored in Live's memory. It does not prove anything was heard "
            "(docs/limits.md, section 6). The device may be off, the track muted, a clip envelope "
            "may be overriding the parameter during playback, and nothing has been saved to disk. "
            "Check track.mute and device.is_active, check whether a clip envelope targets that "
            "parameter, then play it in Live and listen."
        ),
        evidence=(
            "docs/limits.md, section 6, 'What a success result actually proves'; rows track.mute "
            "and device.is_active in src/live_maestro/catalog/20-track.yaml and 40-device.yaml"
        ),
        next_call=(
            "track.mute and device.is_active, then read_automation for an overriding envelope, "
            "then play"
        ),
    ),
    RouteEntry(
        terms=(
            "als file", "the als", "project file on disk", "read the set from disk",
            "edit the file while live", "while live is open", "open project file", "gzip xml",
        ),
        answer=(
            "Channel B reads and writes the .als on disk, and that file is the last manual save "
            "rather than what is in Live right now (docs/limits.md, section 1, 'Saving the project "
            "(LOM)'). als_write refuses if that set is open in Live, because a write to a "
            "set Live has open is lost without a word the next time Live saves, and it refuses "
            "again if the file was written seconds ago. Save in Live before reading the file, and "
            "close the set in Live before writing it; a backup is taken first and can be put back "
            "with the restore operation."
        ),
        evidence=(
            "docs/limits.md, section 1, 'Saving the project (LOM)'; docs/architecture.md, "
            "'Three channels into Live'; src/live_maestro/als/write.py, _guard and _backup"
        ),
        next_call="als_read after saving in Live; als_write only with the set closed in Live",
    ),
)

#: Intents an existing tool already serves, usually with verification a raw row has no
#: way to provide. Sending a caller to lom_set here would be a downgrade.
INTENT_TOOL: tuple[RouteEntry, ...] = (
    RouteEntry(
        terms=(
            "lufs", "loudness", "how loud is", "measure volume", "measure the audio",
            "true peak", "headroom", "check phase", "mono compatibility", "frequency balance",
            "spectral balance", "is it clipping", "audio analysis", "analyze audio",
        ),
        answer=(
            "Channel C (LiveMaestro Analyzer) measures real-time acoustic properties "
            "directly from the Master track or any channel holding LiveMaestro_Analyzer.amxd: "
            "integrated and short-term LUFS (EBU R128), True-Peak with oversampling, stereo phase "
            "correlation and low-end mono compatibility, and 7-band frequency balance."
        ),
        evidence="src/live_maestro/analyzer/client.py; devices/LiveMaestro_Analyzer.amxd",
        next_call="analyze_audio",
    ),
    RouteEntry(
        terms=(
            "a bit", "a little", "a touch", "bit louder", "bit quieter", "couple of db",
            "relative",
        ),
        answer=(
            "There is no relative write in Live's API: every mixer value is absolute. Read the "
            "current value with get_track, do the arithmetic yourself, then write the result with "
            "set_mix, which reports before, after and Live's own dB display."
        ),
        evidence=(
            "src/live_maestro/server.py, set_mix docstring: 'Read the current state with "
            "get_track first, since the values here are absolute and not relative'"
        ),
        next_call="get_track, then set_mix",
    ),
    RouteEntry(
        terms=(
            "sidechain", "side chain", "duck", "ducking",
        ),
        answer=(
            "Sidechain hangs on the device, not on the track, which is why looking beside "
            "track.input_routing_type finds nothing and why track.mute is never the answer. Write "
            "device.input_routing_type by object reference out of device.input_routing_types, pick "
            "the tap point with device.input_routing_channel, then arm the compressor's S/C On "
            "with set_parameter after finding its index with describe(with_parameters=True). For a "
            "set Live does not have open, als_write with operation='sidechain' wires it in the "
            "file."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, section header 'Device input routing - "
            "the sidechain source, which is reachable after all' and row device.input_routing_type "
            "(write verified 2026-08-30); docs/limits.md section 2, 'Sidechain routing "
            "configuration'"
        ),
        next_call=(
            "describe(with_parameters=True) then set_parameter; lom_set on "
            "device.input_routing_type; als_write(operation='sidechain') for a closed set"
        ),
    ),
    RouteEntry(
        terms=(
            "configure", "configure mode", "only device on", "shows device on", "no parameters",
            "one parameter", "not exposed",
        ),
        answer=(
            "A third-party plug-in showing a single parameter has not been through Live's "
            "Configure mode: the LOM sees the parameter strip, not the plug-in. get_devices flags "
            "it as configure_needed, describe reports the real count and probes indices when "
            "Live's Vector gives none, lom_call(device, 'get_parameter_names') lists what the "
            "plug-in could expose, and als_write with operation='configure' fills the empty slots "
            "of a saved set, which no LOM call can do."
        ),
        evidence=(
            "docs/limits.md section 3, 'Third-party VST parameter exposure'; "
            "src/live_maestro/server.py, CONFIGURE_NOTE and the als_write docstring ('configure "
            "fills a plugin's parameter strip, which no LOM call can do')"
        ),
        next_call=(
            "get_devices, then describe(with_parameters=True); als_write(operation='configure') on "
            "a closed set"
        ),
    ),
    RouteEntry(
        terms=(
            "als", "project file", "saved set", "on disk", "without opening", "offline",
            "backup",
        ),
        answer=(
            "A .als on disk is read by als_read and edited by als_write; no LOM path reaches a "
            "file Live does not have open, and nothing Live holds in memory is in the file until "
            "the set is saved. als_write refuses the set Live currently holds, because that edit "
            "would sit on disk only until Live's next save."
        ),
        evidence=(
            "src/live_maestro/server.py, als_read docstring with SAVED_STATE_NOTE, and the "
            "als_write docstring plus _set_open_in_live"
        ),
        next_call="als_read; als_write(confirm=True) for edits",
    ),
    RouteEntry(
        terms=(
            "read the automation", "what automation", "is it automated", "check the automation",
            "sample the envelope", "has envelopes",
        ),
        answer=(
            "read_automation samples one DeviceParameter's stored envelope in a Session clip and "
            "says whether an envelope exists at all. The LOM cannot enumerate breakpoints, so the "
            "answer is samples rather than support points; for real breakpoints read the saved set "
            "with als_read. Readings at or near beat 0 return the parameter default, not the "
            "curve."
        ),
        evidence=(
            "src/live_maestro/server.py, read_automation docstring and its returned notes; "
            "src/live_maestro/catalog/30-clip.yaml, row clip.automation_envelope ('at time 0 "
            "Live returns the parameter's default value, not the curve')"
        ),
        next_call="read_automation",
    ),
    RouteEntry(
        terms=(
            "clear the automation", "delete the automation", "remove the automation",
            "get rid of the automation", "unautomate", "clear the envelope",
            "delete the envelope",
        ),
        answer=(
            "clear_automation removes an envelope and, with confirm=False, first reports which "
            "envelopes the clip holds and what would be lost. Prefer it to the clip.clear_envelope "
            "and clip.clear_all_envelopes rows: the wide one takes curves on devices nobody was "
            "thinking about, and a cleared clip envelope does not tidy anything, it uncovers the "
            "Arrangement track automation underneath. To change a curve rather than remove it, "
            "write_automation with clear_first=True needs no clearing at all."
        ),
        evidence=(
            "src/live_maestro/server.py, clear_automation docstring; "
            "src/live_maestro/catalog/30-clip.yaml, row clip.clear_all_envelopes; "
            "docs/limits.md section 2, 'Track automation vs. clip envelopes'"
        ),
        next_call=(
            "clear_automation(confirm=True), or write_automation(clear_first=True) to replace"
        ),
    ),
    RouteEntry(
        terms=(
            "automation", "envelope", "sweep", "ramp", "fade in", "fade out",
            "ride the", "over time", "gradually", "riser",
        ),
        answer=(
            "write_automation turns breakpoints into a stored curve and reads it back, so the "
            "result reports the stored envelope rather than the requested one. Automation is "
            "writable only in Session clips: write the curve into the Session clip and then copy "
            "it onto the timeline with arrange, which carries the envelope with it. Whoever "
            "arranges first and writes the curve afterwards ends up with the curve in the Session "
            "clip and in no Arrangement clip at all, and read_automation confirms it because it "
            "reads the Session clip."
        ),
        evidence=(
            "src/live_maestro/server.py, write_automation docstring with SESSION_ONLY_NOTE; "
            "src/live_maestro/catalog/30-clip.yaml, row arrangement_clip.duplicate_from_session "
            "('Measured trap, the expensive one')"
        ),
        next_call="write_automation, then arrange",
    ),
    RouteEntry(
        terms=(
            "arrange", "into the arrangement", "to the arrangement", "onto the timeline",
            "into the timeline", "session to arrangement", "on the timeline",
        ),
        answer=(
            "arrange copies a Session clip onto the Arrangement timeline at a beat and counts that "
            "track's arrangement clips before and after, because the LOM reports nothing about the "
            "new clip. It is a copy and not a move, and calling it twice adds a second copy at the "
            "same beat rather than replacing the first. Prefer it to the raw call: a time argument "
            "spelled anything but destination_time falls back silently to 0.0 and reports success."
        ),
        evidence=(
            "src/live_maestro/server.py, arrange docstring; "
            "src/live_maestro/catalog/30-clip.yaml, row arrangement_clip.duplicate_from_session"
        ),
        next_call="arrange",
    ),
    RouteEntry(
        terms=(
            "locator", "cue point", "drop a marker", "add a marker", "place a marker",
            "section marker",
        ),
        answer=(
            "set_locator places an Arrangement cue locator, and with confirm=False it first lists "
            "the locators that already exist. Prefer it to the song.set_or_delete_cue row, which "
            "is a toggle with no position argument: the same arguments twice leave nothing behind. "
            "The tool writes song.current_song_time first, then names the new locator through the "
            "cue rows."
        ),
        evidence=(
            "src/live_maestro/server.py, set_locator docstring; "
            "src/live_maestro/catalog/10-song.yaml, row song.set_or_delete_cue ('Marked "
            "destructive because it is a toggle') and rows cue.time, cue.name"
        ),
        next_call="set_locator(at_beat=..., confirm=True)",
    ),
    RouteEntry(
        terms=(
            "loop", "looping", "loop brace", "loop region", "loop points", "loop bars",
        ),
        answer=(
            "Two different loops. The Arrangement transport brace is set_loop, where length is a "
            "duration in beats and not an end beat, so bars 5 to 9 in 4/4 is start=16, length=16; "
            "calling it with no arguments simply reports the current brace. A Session clip's own "
            "loop is a clip property with no bearing on the brace: read it with get_clip and write "
            "clip.looping, clip.loop_start and clip.loop_end."
        ),
        evidence=(
            "src/live_maestro/server.py, set_loop docstring ('The brace only loops the "
            "Arrangement transport... which is a clip property; read that with get_clip'); catalog "
            "rows song.loop_start and song.loop_length in 10-song.yaml, clip.looping, "
            "clip.loop_start and clip.loop_end in 30-clip.yaml"
        ),
        next_call=(
            "set_loop for the Arrangement brace; get_clip then lom_set on clip.looping for a clip"
        ),
    ),
    RouteEntry(
        terms=(
            "quantize", "quantise", "tighten", "snap", "on the grid", "lock to the grid",
            "timing",
        ),
        answer=(
            "quantize_clip reads the notes, snaps them and verifies the write, and it takes the "
            "grid in beats: 1.0 a quarter, 0.25 a sixteenth. Prefer it to the clip.quantize row, "
            "which takes Live's grid enum as an integer whose mapping the catalog explicitly does "
            "not assert, and whose note count cannot change and therefore proves nothing. Use "
            "strength below 1.0 to keep the feel, and read the notes first, because quantising is "
            "not undoable from here."
        ),
        evidence=(
            "src/live_maestro/server.py, quantize_clip docstring and grid argument; "
            "src/live_maestro/catalog/30-clip.yaml, row clip.quantize ('quantization_grid is "
            "Live's grid enum as an integer (mapping not asserted here)')"
        ),
        next_call="read_clip_notes, then quantize_clip",
    ),
    RouteEntry(
        terms=(
            "transpose", "octave", "semitone", "pitch it up", "pitch it down", "up an octave",
            "down an octave", "repitch",
        ),
        answer=(
            "transpose_clip moves a MIDI clip by rewriting every note pitch and an audio clip by "
            "writing clip.pitch_coarse. It is relative on both paths, so calling it twice with 2 "
            "moves the clip by 4 and there is no absolute target to aim at. Keep "
            "out_of_range='error' unless the notes have been read out first: clamp collapses notes "
            "onto one pitch and drop loses them, and transposing back returns neither."
        ),
        evidence=(
            "src/live_maestro/server.py, transpose_clip docstring; "
            "src/live_maestro/catalog/30-clip.yaml, row clip.pitch_coarse (audio clips only, "
            "-48..48, measured 2026-08-29 against Live 12.4.5)"
        ),
        next_call="transpose_clip",
    ),
    RouteEntry(
        terms=(
            "how many notes", "note count", "count the notes", "read the notes", "what notes",
            "which notes", "list the notes", "get the notes",
        ),
        answer=(
            "read_clip_notes reads a Session clip's MIDI and can take a window (from_time plus "
            "time_span, a length in beats and not an end beat) or count_only, which counts inside "
            "Live and carries back one integer. The LOM has no note_count on Clip at all, and one "
            "drum clip came back as 384 notes and 57k characters. It sorts by time then pitch, "
            "because Live returns notes ordered by pitch, which is the one order a musical "
            "instruction never means."
        ),
        evidence=(
            "src/live_maestro/server.py, read_clip_notes docstring and its count_only argument"
        ),
        next_call="read_clip_notes",
    ),
    RouteEntry(
        terms=(
            "melody", "bassline", "bass line", "riff", "chord", "chords", "arpeggio",
            "midi notes", "write notes", "add notes", "program a", "topline", "lead line",
        ),
        answer=(
            "write_clip_notes writes MIDI into a Session clip and defaults to replace, which is "
            "remove-then-write inside one handler call. Never reach for the clip.set_notes row: it "
            "is catalogued broken and refused by the script's allowlist, it appends rather than "
            "replaces while reporting the length of the list it was given, and it opens a modal "
            "dialog that blocks Live's main thread. A second call there is not a correction, it is "
            "a duplication that reports success: 63 notes plus 23 written left 86 in the clip."
        ),
        evidence=(
            "src/live_maestro/catalog/30-clip.yaml, row clip.set_notes (status broken, "
            "'MEASURED TRAP - the name is wrong'); src/live_maestro/server.py, write_clip_notes "
            "docstring and NOTES_REPLACE_NOTE"
        ),
        next_call="create_clip if the slot is empty, then write_clip_notes",
    ),
    RouteEntry(
        terms=(
            "new clip", "empty clip", "create a clip", "make a clip", "blank clip",
            "add a clip", "clip slot",
        ),
        answer=(
            "create_clip makes an empty MIDI clip of a length in beats and refuses an occupied "
            "slot rather than overwriting it, so nothing is lost by aiming at the wrong slot. "
            "length_beats is beats and not bars, so one bar of 4/4 is 4.0, and a fresh set has 8 "
            "scenes, so slot 8 is out of range. The clip arrives empty; fill it with "
            "write_clip_notes."
        ),
        evidence=(
            "src/live_maestro/server.py, create_clip docstring; "
            "src/live_maestro/catalog/30-clip.yaml, row clip_slot.create_clip ('Measured trap: "
            "create_clip does NOT overwrite')"
        ),
        next_call="create_clip, then write_clip_notes",
    ),
    RouteEntry(
        terms=(
            "new track", "add a track", "create a track", "another track", "return track",
            "midi track", "audio track",
        ),
        answer=(
            "create_track adds a MIDI, audio or return track, counts the tracks before and after "
            "and hands back the index it landed on, which the LOM itself does not report. Append "
            "with index -1: inserting anywhere else shifts every later track, and an index is a "
            "position rather than an identity, so every index cached from an earlier survey is "
            "then wrong with no error saying so. The new track carries no instrument."
        ),
        evidence=(
            "src/live_maestro/server.py, create_track docstring and STALE_INDEX_NOTE; "
            "src/live_maestro/catalog/10-song.yaml, row song.create_midi_track ('What it "
            "returns was not recorded')"
        ),
        next_call="create_track, then load_device",
    ),
    RouteEntry(
        terms=(
            "load", "plugin", "vst", "preset", "browser", "instrument", "synth",
        ),
        answer=(
            "load_device searches the browser and, with confirm=True, loads onto the track Live "
            "has selected, because Browser.load_item takes no track argument. An effect is "
            "appended, but an instrument replaces the instrument already on the track and discards "
            "it silently with every parameter configured on it and every clip envelope pointing at "
            "it, so the tool reports chain_before, chain_after and what it displaced. Pace the "
            "loads: loading plug-ins in rapid succession has taken Live down."
        ),
        evidence=(
            "src/live_maestro/server.py, load_device docstring; "
            "src/live_maestro/catalog/50-browser.yaml, row browser.load_item (destructive, 'IT "
            "TAKES NO TRACK ARGUMENT')"
        ),
        next_call=(
            "get_devices to check the chain, then load_device(query=...) and load_device(uri=..., "
            "confirm=True)"
        ),
    ),
    RouteEntry(
        terms=(
            "what devices", "which devices", "device chain", "what plugins", "list the devices",
            "effects chain", "signal chain",
        ),
        answer=(
            "get_devices lists a track's chain with the indices set_parameter and delete_device "
            "are addressed by, and flags a third-party plug-in exposing exactly one parameter as "
            "configure_needed. It counts parameters and does not list them; for the names, ranges "
            "and display units call describe with with_parameters=True."
        ),
        evidence=(
            "src/live_maestro/server.py, get_devices docstring; "
            "src/live_maestro/catalog/40-device.yaml, rows device.list and device.name"
        ),
        next_call="get_devices",
    ),
    RouteEntry(
        terms=(
            "what parameters", "which parameters", "parameter names", "list the parameters",
            "min and max", "parameter range", "what knobs",
        ),
        answer=(
            "describe with with_parameters=True surveys one device's parameters with value, min, "
            "max and display, and falls back to probing indices when Live's Vector reports no "
            "count, which is how a plug-in with parameters gets reported as having none. Prefer it "
            "to lom_describe for a device. value lies on the device's own scale and display "
            "carries the physical reading: measured on a Max device, an LPF value of 8794.30 in a "
            "range of 20 to 21000 displayed 288 Hz."
        ),
        evidence=(
            "src/live_maestro/server.py, describe docstring and its with_parameters notes "
            "(measured 2026-08-30 against Live 12.4.5)"
        ),
        next_call="describe(path='song.tracks[0].devices[1]', with_parameters=True)",
    ),
    RouteEntry(
        terms=(
            "fader", "volume", "louder", "quieter", "db", "decibel", "gain staging",
            "turn it down", "turn it up",
        ),
        answer=(
            "set_mix writes volume, pan, sends, mute and solo on one track in a single round trip "
            "and reports before, after and the dB display Live offered. Aim there rather than at "
            "track.volume: that row is the parameter object and answers not_settable, and only "
            "track.volume_value is writable. The scale is normalised and not decibels, 0.85 is 0 "
            "dB, and 0.425 is not half of anything. A knob on a device in the chain is "
            "set_parameter instead."
        ),
        evidence=(
            "src/live_maestro/catalog/20-track.yaml, rows track.volume and track.volume_value "
            "('the only way to WRITE it: lom_set on the parameter object itself answers "
            "not_settable'); src/live_maestro/server.py, set_mix docstring"
        ),
        next_call="get_track, then set_mix(volume=...)",
    ),
    RouteEntry(
        terms=(
            "pan", "panning", "hard left", "hard right", "stereo position", "centre it",
            "center it",
        ),
        answer=(
            "Pan is set_mix with pan from -1.0 hard left through 0.0 centre to 1.0 hard right. "
            "Write it there rather than at track.panning, which is the parameter object and not "
            "settable; track.panning_value is the writable companion and set_mix carries the same "
            "write with its read-back."
        ),
        evidence=(
            "src/live_maestro/catalog/20-track.yaml, rows track.panning and "
            "track.panning_value; src/live_maestro/server.py, set_mix pan argument"
        ),
        next_call="set_mix(pan=...)",
    ),
    RouteEntry(
        terms=(
            "send", "sends", "aux", "reverb send", "delay send", "more reverb", "more delay",
            "fx send",
        ),
        answer=(
            "A send level is set_mix with sends, keyed by the letter Live shows ('A', 'B') or by "
            "index as a string, with normalised values, and only the sends named are written. The "
            "raw row is track.send_value; the parameter object track.send is not settable, and "
            "set_mix does the letter-to-index resolution and the read-back for you."
        ),
        evidence=(
            "src/live_maestro/server.py, set_mix sends argument; "
            "src/live_maestro/catalog/20-track.yaml, rows track.send and track.send_value"
        ),
        next_call="set_mix(sends={'A': 0.3})",
    ),
    RouteEntry(
        terms=(
            "ms", "milliseconds", "hertz", "hz", "khz", "normalised", "normalized",
            "physical units", "display units",
        ),
        answer=(
            "A device knob is written with set_parameter in the parameter's own units, which are "
            "normalised and device-specific rather than the unit the device displays, and the "
            "curve between the two is not linear. Read min, max and display with describe first "
            "and aim with the display: on one third-party compressor Attack is v^4 * 1000 ms, so "
            "writing '10 ms' linearly lands at 316 ms, a factor of thirty, silently. A quantized "
            "parameter takes discrete steps, so a written 0.5 reading back differently is a clamp "
            "and not a failure."
        ),
        evidence=(
            "src/live_maestro/server.py, set_parameter docstring and the comment above it "
            "(measured 2026-08-30 against Live 12.4.5); docs/limits.md section 3, 'Normalized "
            "value vs. physical display value'"
        ),
        next_call="describe(with_parameters=True), then set_parameter",
    ),
    RouteEntry(
        terms=(
            "bpm", "tempo", "beats per minute",
        ),
        answer=(
            "set_tempo writes song.tempo and reads it back, and BPM is a real unit here, which "
            "makes this the exception among the setters. One tempo governs the whole set and there "
            "is no per-track tempo. It does not survive every scene launch: firing a scene whose "
            "tempo_enabled is on writes song.tempo again from scene.tempo, so read the tempo back "
            "after a launch rather than assuming this one held."
        ),
        evidence=(
            "src/live_maestro/server.py, set_tempo docstring; "
            "src/live_maestro/catalog/10-song.yaml, rows song.tempo, scene.tempo and "
            "scene.tempo_enabled"
        ),
        next_call="set_tempo(bpm=...)",
    ),
    RouteEntry(
        terms=(
            "play", "stop", "transport", "hit play", "press play", "start playback",
            "stop playback",
        ),
        answer=(
            "play starts the transport and reads is_playing back; stop halts it, and stop with "
            "clips=True is what also stops the playing Session clips, because stopping the "
            "transport alone does not. Launching or stopping one Session clip is a different thing "
            "and has no tool: those are the clip_slot.fire and clip_slot.stop rows, and whether a "
            "clip is playing is clip.is_playing. A running transport proves nothing is audible."
        ),
        evidence=(
            "src/live_maestro/server.py, play and stop docstrings; "
            "src/live_maestro/catalog/10-song.yaml, rows song.start_playing and "
            "song.stop_playing; 30-clip.yaml, rows clip_slot.fire, clip_slot.stop and "
            "clip.is_playing"
        ),
        next_call="play; stop(clips=True); lom_call on clip_slot.fire for one clip",
    ),
    RouteEntry(
        terms=(
            "playhead", "go to bar", "jump to bar", "move the playhead", "start from bar",
            "from the top",
        ),
        answer=(
            "The Arrangement playhead is song.current_song_time in beats: set_arrangement_time "
            "moves it without starting anything, and play with from_beat cues and starts in one "
            "call. Do not sample a moving transport through lom_batch, because a read that follows "
            "a jump inside the same batch still reports the position from before the move, and "
            "reports it without an error."
        ),
        evidence=(
            "src/live_maestro/server.py, set_arrangement_time docstring and the play from_beat "
            "argument; src/live_maestro/catalog/10-song.yaml, row song.current_song_time"
        ),
        next_call="set_arrangement_time(at_beat=...), or play(from_beat=...)",
    ),
    RouteEntry(
        terms=(
            "what tracks", "how many tracks", "show me the set", "what is in the set",
            "session overview", "survey", "track list", "what is loaded",
        ),
        answer=(
            "get_session is the one expensive read, about 450 ms for the handshake, and it hands "
            "back the track and scene indices every other tool here is addressed by. Take it once "
            "at the start and narrow with get_track, get_clip and get_devices rather than calling "
            "it again between edits. It is a photograph of Live's memory: it goes stale the moment "
            "a human touches the mouse, it covers the Session grid only, and it says nothing about "
            "the .als on disk."
        ),
        evidence=(
            "src/live_maestro/server.py, get_session docstring with SESSION_ONLY_SURVEY_NOTE "
            "and GROUP_TRACK_NOTE"
        ),
        next_call="get_session, then get_track / get_clip / get_devices",
    ),
    RouteEntry(
        terms=(
            "warp", "warping", "warp mode", "warped", "timestretch", "time stretch",
        ),
        answer=(
            "get_clip reads warping, warp_mode, pitch_coarse and the clip's times in one round "
            "trip and checks the slot is filled first; the writes are the clip.warping and "
            "clip.warp_mode rows through lom_set, and clip.available_warp_modes is the "
            "authoritative per-clip list because the integer mapping behind warp_mode is still a "
            "hypothesis. Call get_clip again after writing warping: on a measured 1.14-unit audio "
            "clip at 120 BPM, turning warping on doubled loop_end and end_marker while length did "
            "not follow, so every time field read before that write is invalid."
        ),
        evidence=(
            "src/live_maestro/server.py, get_clip docstring; "
            "src/live_maestro/catalog/30-clip.yaml, rows clip.warping, clip.warp_mode and "
            "clip.available_warp_modes"
        ),
        next_call="get_clip; lom_set on clip.warping, then get_clip again",
    ),
)

#: Intents whose answer is a runtime DeviceParameter under devices[i].parameters[N].
#: The catalog cannot enumerate these: what a device exposes depends on the device
#: (docs/architecture.md, 'fixed and dynamic surface'). Consulted only after row scoring
#: has failed to find anything confident, because these triggers are deliberately broad:
#: "reverb" belongs to a device parameter and also to a return track's send level, and
#: the row is the better answer when a row scores well.
DEVICE_PARAMETER: tuple[RouteEntry, ...] = (
    RouteEntry(
        terms=(
            "device on", "configure", "unconfigured", "no parameters", "only one parameter",
            "parameter strip", "expose", "vst2", "vst3", "plugin parameters",
        ),
        answer=(
            "A plug-in whose strip holds only 'Device On' is unconfigured, not uncontrollable: "
            "nothing has been taken into Live's parameter strip for that instance yet. "
            "get_devices(track) reports parameter_count and raises configure_needed for exactly "
            "that case; describe(device path, with_parameters=True) lists what is actually there; "
            "and lom_call on device.get_parameter_names returns what the plug-in itself declares, "
            "which can be hundreds while the strip holds one. On load with no GUI interaction, two "
            "VST3 instruments reported 1 parameter, another VST3 instrument 29, a VST3 effect 16 "
            "and a VST2 effect 17: read the count, never predict it. Only Live's Configure mode "
            "adds knobs (GUI only, 128 slots per instance, stored in the set, so a second instance "
            "starts empty)."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows device.parameters and "
            "device.get_parameter_names; docs/limits.md §3, 'Third-party VST parameter exposure "
            "(Live)'; CONFIGURE_NOTE and get_devices in src/live_maestro/server.py"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> lom_call on catalog row device.get_parameter_names when the "
            "strip is empty"
        ),
    ),
    RouteEntry(
        terms=(
            "cutoff", "filter", "frequency", "lpf", "hpf", "lowpass", "highpass", "low pass",
            "high pass", "bandpass", "brighter", "darker", "muffled",
        ),
        answer=(
            "A filter cutoff is a runtime DeviceParameter at devices[i].parameters[N], not a "
            "catalog row: the word 'cutoff' appears nowhere in the catalog and the parameter index "
            "is not fixed. Three calls: get_devices(track) for the device index, describe(device "
            "path, with_parameters=True) for the parameter names, indices, min, max and display, "
            "then set_parameter(track, device, parameter='Frequency', value=...). The value is the "
            "parameter's own normalised scale, not hertz. For an EQ Eight or Auto Filter frequency "
            "the scale is measured: 0..1 maps logarithmically over 20 Hz..20 kHz, so norm = "
            "log10(hz/20)/3 and hz = 20*10**(3*norm) (catalog row eq8.frequency_hint_value). Do "
            "not use the 30 Hz variant found in other scripts: aiming at 100 Hz with it lands on "
            "71.9 Hz, 572 cents off, with no error."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows eq8.frequency_hint and "
            "eq8.frequency_hint_value (the measured 20 Hz formula) and param.value; "
            "docs/architecture.md, 'Fixed and dynamic surface'"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='Frequency', "
            "value=0.62)"
        ),
    ),
    RouteEntry(
        terms=(
            "resonance", "reso", "q", "emphasis", "squelch",
        ),
        answer=(
            "Resonance is a strip parameter under devices[i].parameters[N], which the catalog "
            "cannot enumerate because what a device exposes is decided at runtime. Find it with "
            "get_devices(track) then describe(device path, with_parameters=True), then write it "
            "with set_parameter(track, device, parameter='Reso*', value=...); a glob that matches "
            "more than one parameter is refused rather than guessed at. Values are normalised, not "
            "a Q number. If the write reports no movement, check param.state: 'irrelevant' is "
            "exactly the case of a filter's resonance while the filter is bypassed, and it means "
            "the device configuration makes the control meaningless right now."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows param.state ('a filter's resonance "
            "while the filter is bypassed') and param.value; set_parameter's parameter argument in "
            "src/live_maestro/server.py"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='Reso*', "
            "value=0.3)"
        ),
    ),
    RouteEntry(
        terms=(
            "threshold", "ratio", "knee", "makeup", "make up", "compressor", "compression",
            "limiter", "expander", "gain reduction", "squash", "glue",
        ),
        answer=(
            "Compressor controls live on the device's runtime parameter strip, not in the catalog: "
            "get_devices(track) for the device index, describe(device path, with_parameters=True) "
            "for names, indices, min and max, then set_parameter(track, device, "
            "parameter='Threshold', value=...). Do not assume 0..1: Glue Compressor Threshold runs "
            "-40..0 dB, so read min and max first. Time controls on the same device are worse: on "
            "one third-party compressor Attack is v**4 * 1000 ms, so writing '10 ms' linearly "
            "lands at 316 ms, a factor of 30, silently. A plug-in compressor showing only 'Device "
            "On' has an unconfigured strip rather than no controls."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, row param.value (Glue Compressor "
            "Threshold -40..0 dB; the v**4 attack curve) and rows param.min, param.max; "
            "set_parameter's Note in src/live_maestro/server.py"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='Threshold', "
            "value=-18.0)"
        ),
    ),
    RouteEntry(
        terms=(
            "sidechain threshold", "sidechain amount", "sidechain ratio", "sidechain release",
            "s c on", "ducking amount",
        ),
        answer=(
            "Split the question. The routing half is set on the receiving device through "
            "device.input_routing_type and device.input_routing_channel, assigned by object "
            "reference rather than by name. The knob half, including the 'S/C On' switch that arms "
            "it and the threshold, ratio and release that shape it, is a runtime DeviceParameter "
            "addressed by integer index: a quoted name is not a legal path. Find N with "
            "get_devices(track) then describe(device path, with_parameters=True), and write with "
            "set_parameter(track, device, parameter='S/C On', value=1). Values are normalised, not "
            "dB."
        ),
        evidence=(
            "docs/limits.md §2, 'Sidechain routing configuration' (the S/C On parameter, addressed "
            "by index, found with with_parameters=True); "
            "src/live_maestro/catalog/40-device.yaml, row device.input_routing_type"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='S/C On', "
            "value=1)"
        ),
    ),
    RouteEntry(
        terms=(
            "reverb", "damping", "predelay", "pre delay", "room size", "diffusion", "tail",
            "verb", "reverb decay",
        ),
        answer=(
            "Reverb size, decay, damping, predelay and diffusion are runtime parameters on the "
            "strip: get_devices(track) for the device index, describe(device path, "
            "with_parameters=True) for the names and indices, then set_parameter(track, device, "
            "parameter='Decay*', value=...) in the parameter's own normalised units, not seconds. "
            "One exception, and it is class-specific: Hybrid Reverb's convolution section carries "
            "real catalog rows (hybrid_reverb.ir_decay_time, hybrid_reverb.ir_size_factor, "
            "hybrid_reverb.ir_attack_time) which sit on the device object rather than in "
            "parameters[], are read-verified only, and whose unit is not established."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows hybrid_reverb.ir_decay_time and "
            "hybrid_reverb.ir_size_factor (class extensions, no write attempted) and row "
            "param.value"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='Decay*', "
            "value=0.55)"
        ),
    ),
    RouteEntry(
        terms=(
            "attack", "release", "sustain", "decay", "adsr", "envelope", "snappier", "punchier",
        ),
        answer=(
            "Envelope stages are runtime DeviceParameters under devices[i].parameters[N]: "
            "get_devices(track) for the device index, describe(device path, with_parameters=True) "
            "for names, indices, min, max and display, then set_parameter(track, device, "
            "parameter='Attack*', value=...). The value is not milliseconds. Every parameter has "
            "its own curve from its raw range onto its unit and it is not linear: measured on one "
            "third-party compressor, Attack is v**4 * 1000 ms, so a linear 'Attack of 10 ms' sets "
            "316 ms, a factor of 30 off with no error. Where the device reports a display, aim by "
            "the display; where it does not (every VST2), write normalised and calibrate once by "
            "eye."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, row param.value (the v**4 attack curve) "
            "and row param.str_for_value (VST2 reports no units); set_parameter's Note in "
            "src/live_maestro/server.py"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='Attack*', "
            "value=0.18)"
        ),
    ),
    RouteEntry(
        terms=(
            "dry wet", "wet dry", "wetness", "blend", "mix knob", "how wet", "amount knob",
        ),
        answer=(
            "Dry/Wet is a parameter on the device's own strip, so it has no catalog row of its "
            "own: get_devices(track) for the device index, describe(device path, "
            "with_parameters=True) to see the name (Live spells it 'Dry/Wet'), then "
            "set_parameter(track, device, parameter='Dry/Wet', value=...). The value is "
            "normalised, so 0.3 is not necessarily 30 percent of anything until the display says "
            "so; read the display back from the write. On a send effect the same intent is usually "
            "the track send level instead, which is a catalog row rather than this route."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, row param.name (names 'Frequency', "
            "'Dry/Wet', 'Macro 1' as the display names on the strip); describe with_parameters in "
            "src/live_maestro/server.py"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='Dry/Wet', "
            "value=0.25)"
        ),
    ),
    RouteEntry(
        terms=(
            "feedback", "delay time", "repeats", "ping pong", "pingpong", "echo",
            "dotted eighth", "delay sync",
        ),
        answer=(
            "Delay feedback, time and mode are runtime parameters: get_devices(track), "
            "describe(device path, with_parameters=True), then set_parameter(track, device, "
            "parameter='Feedback', value=...) in normalised units. Two measured traps here "
            "specifically. Delay time is often a stepped parameter rather than a sweep: on devices "
            "measured here a tape delay's mode selector had 12 steps, so a written 0.35 "
            "legitimately reads back as 0.25 or 0.50; check param.is_quantized and read "
            "param.value_items for what each step means. And on one BBD delay the size control "
            "changes what the delay-time parameter means mid-automation, so re-read the "
            "display after any mode change."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows param.is_quantized (the 12-step tape "
            "delay selector and the BBD size control) and param.value_items"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='Feedback', "
            "value=0.4)"
        ),
    ),
    RouteEntry(
        terms=(
            "drive", "saturation", "distortion", "overdrive", "crush", "bitcrush", "fuzz",
            "grit", "warmth", "soft clip",
        ),
        answer=(
            "Drive, saturation and bit-crush amounts are runtime DeviceParameters on the strip, "
            "not catalog rows: get_devices(track) for the device index, describe(device path, "
            "with_parameters=True) for names and ranges, then set_parameter(track, device, "
            "parameter='Drive', value=...). Values are normalised and device-specific, not dB of "
            "drive, and min/max are whatever the device declares rather than 0..1. If the "
            "saturator is a third-party plug-in showing only 'Device On', its strip is "
            "unconfigured and nothing can be written until parameters are exposed in Live."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows param.value, param.min and "
            "param.max; docs/limits.md §3, 'Normalized value vs. physical display value (Live)'"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='Drive', "
            "value=0.5)"
        ),
    ),
    RouteEntry(
        terms=(
            "detune", "glide", "portamento", "unison", "octave", "semitone", "fine tune",
            "coarse", "oscillator", "osc", "voices", "polyphony", "spread",
        ),
        answer=(
            "Oscillator tune, detune, glide, unison and voice count are runtime parameters on the "
            "instrument's strip for every synth except the few Live devices that publish a class "
            "extension: get_devices(track), describe(device path, with_parameters=True), then "
            "set_parameter(track, device, parameter='Detune', value=...) in normalised units. The "
            "exceptions are real catalog rows and are Live-device-only: wavetable.unison_mode, "
            "wavetable.unison_voice_count, wavetable.poly_voices, wavetable.mono_poly and "
            "simpler.voices. For Sampler there is no such row: a Sampler is still a Device, so "
            "walk device.parameters and match names."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows wavetable.unison_mode, "
            "wavetable.unison_voice_count, wavetable.poly_voices, simpler.voices and "
            "sampler.parameter_by_name ('walk device.parameters and match names')"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='*Detune*', "
            "value=0.12)"
        ),
    ),
    RouteEntry(
        terms=(
            "lfo", "lfo rate", "lfo depth", "mod depth", "modulation depth", "vibrato",
            "tremolo", "wobble", "auto pan", "autopan",
        ),
        answer=(
            "LFO rate, depth, shape and destination are runtime DeviceParameters at "
            "devices[i].parameters[N]: get_devices(track), describe(device path, "
            "with_parameters=True) for the index, then set_parameter(track, device, "
            "parameter='*Rate*', value=...). The value is normalised, not hertz and not a note "
            "division. A synced rate is usually a stepped parameter, so read param.is_quantized "
            "and param.value_items to learn what step index each division is; on a stepped control "
            "a smooth ramp produces jumps rather than a sweep."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows param.is_quantized and "
            "param.value_items (step labels in index order); row param.value (normalised, not "
            "physical)"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='*Rate*', "
            "value=0.33)"
        ),
    ),
    RouteEntry(
        terms=(
            "chorus", "phaser", "flanger", "widener", "stereo width", "width knob",
            "spread knob", "ensemble",
        ),
        answer=(
            "Modulation-effect controls (rate, depth, width, spread, feedback) are runtime "
            "parameters on the device strip and cannot be catalog rows, because which ones exist "
            "depends on the device: get_devices(track), describe(device path, "
            "with_parameters=True), then set_parameter(track, device, parameter='*Width*', "
            "value=...) in normalised units rather than percent or degrees. Read the returned "
            "display to see what the number means; a device that reports no display at all is "
            "either a VST2, which never reports units, or an instance whose strip holds only "
            "'Device On'."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, row param.str_for_value (VST2 0 of 36 "
            "with real units, VST3 89 of 178, across 214 automatable browser entries); "
            "docs/limits.md §3, 'VST2 display units (Live)'"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='*Width*', "
            "value=0.7)"
        ),
    ),
    RouteEntry(
        terms=(
            "shelf", "bell", "notch", "band gain", "eq band", "band 1", "low shelf",
            "high shelf", "eq curve",
        ),
        answer=(
            "Per-band EQ gain, frequency and Q are runtime parameters under "
            "devices[i].parameters[N]. The catalog carries only EQ Eight's device-level rows "
            "(eq8.global_mode, eq8.oversample, eq8.edit_mode) plus eq8.frequency_hint_value, which "
            "documents the frequency scale and not a fixed index. So: get_devices(track), "
            "describe(device path, with_parameters=True) to find the band's parameter index by "
            "name, then set_parameter. A band frequency is normalised over 20 Hz..20 kHz "
            "logarithmically (norm = log10(hz/20)/3); band gain is on the device's own scale, so "
            "read min and max. Switching eq8.global_mode changes what every band means."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows eq8.frequency_hint ('the parameter "
            "index is not fixed; find it by name in device.parameters'), eq8.frequency_hint_value "
            "and eq8.global_mode"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='2 Frequency A', "
            "value=0.4)"
        ),
    ),
    RouteEntry(
        terms=(
            "waveform", "algorithm", "filter type", "mode selector", "sync division", "detent",
            "stepped", "switch the mode",
        ),
        answer=(
            "Selector-style controls are still DeviceParameters, so the route is the same: "
            "get_devices(track), describe(device path, with_parameters=True), then set_parameter "
            "with the step index as the value. Check param.is_quantized first, because not every "
            "0..1 parameter is continuous: measured on third-party plug-ins, a preamp's EQ "
            "frequency controls had four detents each, a tape delay's mode selector 12, and a "
            "compressor's recovery control fixed steps. For a quantized parameter the raw value is "
            "the step index, param.value_items gives the labels in index order, and no unit "
            "conversion is needed or possible. A written 0.35 reading back as 0.25 is the step "
            "snapping, and set_parameter reports it as a clamp rather than a failure."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows param.is_quantized (the four-detent "
            "preamp and 12-step selector measurements) and param.value_items; set_parameter's "
            "clamp note in src/live_maestro/server.py"
        ),
        next_call=(
            "describe(path='song.tracks[N].devices[i]', with_parameters=True) for value_items -> "
            "set_parameter(track=N, device=i, parameter='Mode', value=3)"
        ),
    ),
    RouteEntry(
        terms=(
            "not enabled", "wont move", "won t move", "refuses", "greyed", "grayed",
            "not settable", "nothing happens", "mapped to a macro", "macro mapped",
        ),
        answer=(
            "When a device parameter will not take a value, three readings say why, all under "
            "devices[i].parameters[N]. param.is_enabled false means something else owns it, in "
            "practice a rack macro: move the macro instead, through rack.macro_value, and note "
            "that macro N is parameters[N] because parameters[0] is 'Device On'. param.state "
            "distinguishes 'irrelevant' (the current device configuration makes the control "
            "meaningless) from 'disabled'. And lom_set on the parameter object always answers "
            "not_settable: the writable path ends in .value (param.value_raw), which is what "
            "set_parameter uses for you. A stored read-back still proves only storage, not "
            "audibility."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows param.is_enabled, param.state, "
            "param.value_raw and rack.macro ('the nested parameters then report is_enabled = "
            "False'); docs/limits.md §6, 'What a success result actually proves'"
        ),
        next_call=(
            "describe(path='song.tracks[N].devices[i]', with_parameters=True) -> lom_get on "
            "param.is_enabled and param.state -> set_parameter, or lom_set on rack.macro_value "
            "when a macro owns it"
        ),
    ),
    RouteEntry(
        terms=(
            "drum pad", "drum rack", "rack chain", "nested", "inside a rack", "per pad",
        ),
        answer=(
            "A knob on a device nested inside a rack chain or a drum pad is still a runtime "
            "DeviceParameter, but set_parameter cannot reach it: its kind argument only covers "
            "track, return and master. Use lom_set on the catalog rows "
            "chain.device_parameter_value "
            "(tracks[t].devices[d].chains[c].devices[n].parameters[p].value) or "
            "drum_pad.device_parameter_value for a single pad's decay, pitch or filter, after "
            "describe(with_parameters=True) on the nested device path to find p. Known structure, "
            "not a probe: a real drum kit preset loads as an InstrumentGroupDevice wrapping the "
            "drum rack, so the rack sits at devices[i].chains[0].devices[0], and the drum_pad path "
            "only resolves for a bare Drum Rack."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows chain.device_parameter_value, "
            "drum_pad.device_parameter and drum_pad.device_parameter_value (the 808 Boom Kit "
            "wrapping structure); _TRACK_KINDS in src/live_maestro/server.py"
        ),
        next_call=(
            "describe(path='song.tracks[N].devices[i].chains[c].devices[n]', with_parameters=True) "
            "-> lom_set on catalog row chain.device_parameter_value (or "
            "drum_pad.device_parameter_value)"
        ),
    ),
    RouteEntry(
        terms=(
            "return track device", "on the return", "return reverb", "master chain",
            "master bus", "master device", "send effect knob",
        ),
        answer=(
            "Devices on a return or the master have their own paths and their own catalog rows, "
            "and the tools take a kind argument for them: get_devices(track=r, kind='return') then "
            "describe(path='song.return_tracks[r].devices[i]', with_parameters=True) then "
            "set_parameter(track=r, device=i, parameter=..., kind='return'), which writes through "
            "return_device.parameter_value; kind='master' writes master_device.parameter_value and "
            "ignores the index. Return tracks are indexed separately from song.tracks and "
            "iterating song.tracks never reaches one. Measured caveat: a return device's "
            "parameters once came back without display_value, so a display-driven search may not "
            "be available there."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows return_device.parameters, "
            "return_device.parameter_value and master_device.parameter_value; _TRACK_KINDS in "
            "src/live_maestro/server.py"
        ),
        next_call=(
            "get_devices(track=r, kind='return') -> "
            "describe(path='song.return_tracks[r].devices[i]', with_parameters=True) -> "
            "set_parameter(track=r, device=i, parameter='Dry/Wet', value=0.5, kind='return')"
        ),
    ),
    RouteEntry(
        terms=(
            "automate the cutoff", "automate a knob", "automate the parameter", "filter sweep",
            "sweep the filter", "automate the filter",
        ),
        answer=(
            "Automating a device knob needs the parameter's index first, and then the parameter "
            "object path rather than the .value path: describe(device path, with_parameters=True) "
            "gives the index, and write_automation takes "
            "parameter='song.tracks[N].devices[i].parameters[p]' (catalog row param.value, which "
            "is the object and is deliberately not settable). Two measured consequences. An "
            "envelope belongs to a device, pointing at device index plus parameter index, so "
            "deleting or reordering devices orphans the curve; putting the automation on a "
            "separate Auto Filter or Utility survives an instrument swap. And after any manual "
            "write to an automated parameter, param.automation_state reads 'overridden' and "
            "playback ignores the curve until param.re_enable_automation is called."
        ),
        evidence=(
            "src/live_maestro/catalog/40-device.yaml, rows param.value (the correct target for "
            "automation_write's parameter argument), param.automation_state and "
            "param.re_enable_automation; ParameterPathArg in src/live_maestro/toolargs.py"
        ),
        next_call=(
            "describe(path='song.tracks[N].devices[i]', with_parameters=True) -> "
            "write_automation(track=N, slot=s, "
            "parameter='song.tracks[N].devices[i].parameters[p]', points=[[0,0.2],[4,0.9]])"
        ),
    ),
    RouteEntry(
        terms=(
            "normalised", "normalized", "what unit", "which unit", "unit", "units",
            "display value", "raw value", "0 to 1",
        ),
        answer=(
            "A device parameter's value is on the device's own scale, bounded by its min and max, "
            "and the display carries the physical reading: the two coincide for linear controls "
            "and diverge for logarithmic ones. Measured against Live 12.4.5: Utility Bass Freq "
            "0.3802 of 0..1 displays 120 Hz, Utility Output -0.171875 of -1..1 displays -6.02 dB, "
            "and a Max device LPF value 8794.30 of 20..21000 displays 288 Hz, out by a factor of "
            "thirty if read as hertz. So aim in value units, read the display back, and use "
            "param.str_for_value to find out whether the device says anything at all: it moves "
            "nothing. Ask it at values strictly inside the range, never at param.min or "
            "param.max: passing an endpoint has taken Live down inside a plugin's own "
            "formatting code, where no try/except reaches it. A VST2 never reports units "
            "(0 of 36 measured), so a display-driven search there walks the control to its end "
            "stop. set_parameter_display does this properly and refuses a device without units."
        ),
        evidence=(
            "docs/limits.md §3, 'Normalized value vs. physical display value (Live)' and "
            "'Asking for a display string at an endpoint (Live)'; "
            "src/live_maestro/catalog/40-device.yaml, rows param.str_for_value, param.min and "
            "param.max; METHOD_ALLOWLIST note on DeviceParameter.str_for_value in "
            "live-remote-script/__init__.py; NORMALISED_NOTE in src/live_maestro/server.py"
        ),
        next_call=(
            "set_parameter_display(track=N, device=i, parameter='Threshold', target=-26, "
            "unit='dB') to aim at the reading itself, or "
            "describe(path='song.tracks[N].devices[i]', with_parameters=True) for value, min, max, "
            "display and unit -> lom_call on catalog row param.str_for_value to test a value "
            "without writing it"
        ),
    ),
    RouteEntry(
        terms=(
            "knob", "knobs", "parameter", "parameters", "device knob", "which parameters",
            "list the parameters",
        ),
        answer=(
            "Which knobs a device has is a runtime fact, not a catalog row: the catalog describes "
            "the shape of Device and DeviceParameter, and what a loaded plug-in exposes is decided "
            "at load time and by what the user took into Configure mode. The procedure is always "
            "the same three calls: get_devices(track) for the chain and its indices, "
            "describe(path='song.tracks[N].devices[i]', with_parameters=True) for every parameter "
            "with name, index, value, min, max, steps and display, then set_parameter(track, "
            "device, parameter=<name or index>, value=...) which verifies the read-back. Two "
            "traps: values are normalised and device-specific rather than Hz or dB, and a device "
            "reporting only 'Device On' has an unconfigured parameter strip, not an empty plug-in."
        ),
        evidence=(
            "docs/architecture.md, 'Fixed and dynamic surface'; "
            "src/live_maestro/catalog/40-device.yaml, row device.parameters, which records "
            "that the strip is not what the plug-in can do; get_devices, describe and "
            "set_parameter in "
            "src/live_maestro/server.py"
        ),
        next_call=(
            "get_devices(track=N) -> describe(path='song.tracks[N].devices[i]', "
            "with_parameters=True) -> set_parameter(track=N, device=i, parameter='<name>', "
            "value=<v>)"
        ),
    ),
)

#: Producer word to catalog words. Only entries that bridge a gap plain matching cannot:
#: "solo" already reaches track.solo and needs no entry, "fader" reaches nothing. Every
#: anchor names a row whose status is 'verified', and where the property is a read-only
#: parameter object the anchor is the *_value companion that can be written.
_SYNONYM_TABLE: tuple[Synonym, ...] = (
    # -- mixing ----------------------------------------------------------------
    Synonym(
        producer=(
            "fader", "faders", "channel fader", "fader level", "pull the fader down",
            "push the fader up", "fader position", "move the fader",
        ),
        catalog=(
            "volume", "volume_value", "mixer_device", "value", "db",
        ),
        anchors=(
            "track.volume_value", "return.volume_value", "master.volume_value",
            "chain.volume_value",
        ),
    ),
    Synonym(
        producer=(
            "level", "levels", "set the level", "turn it down", "turn it up", "turn down the",
            "bring it up", "bring it down", "lower it", "raise it", "louder", "quieter",
            "softer", "drop the level", "too loud", "too quiet",
        ),
        catalog=(
            "volume", "volume_value", "mixer_device", "fader", "level",
        ),
        anchors=(
            "track.volume_value", "return.volume_value", "master.volume_value",
            "chain.volume_value",
        ),
    ),
    Synonym(
        producer=(
            "gain", "trim", "gain staging", "gain stage", "attenuate", "boost", "cut it back",
            "level match", "match the levels", "pad it down",
        ),
        catalog=(
            "volume", "volume_value", "mixer_device", "gain staging", "gain", "db",
        ),
        anchors=(
            "track.volume_value", "master.volume_value", "return.volume_value",
            "chain.volume_value", "clip.gain", "clip.gain_display_string",
        ),
    ),
    Synonym(
        producer=(
            "how many db", "in decibels", "decibels", "minus six", "minus 6", "6 db down",
            "3 db up", "set it to -12", "readable number", "real units", "what does it say",
        ),
        catalog=(
            "db", "str_for_value", "display", "display_value", "gain_display_string", "volume",
        ),
        anchors=(
            "track.volume_str_for_value", "track.volume_display_value",
            "clip.gain_display_string", "track.volume_value", "master.volume_value",
        ),
    ),
    Synonym(
        producer=(
            "headroom", "clipping", "clipped", "too hot", "hot signal", "peaking", "in the red",
            "red lights", "overs", "distorting", "running out of headroom", "is it overloading",
        ),
        catalog=(
            "output_meter_level", "output_meter_left", "output_meter_right", "meter", "output",
            "normalised",
        ),
        anchors=(
            "track.output_meter_level", "master.output_meter_level", "track.output_meter_left",
            "track.output_meter_right", "return.output_meter_level",
        ),
    ),
    Synonym(
        producer=(
            "meter", "metering", "vu", "vu meter", "peak", "peak level", "is it making sound",
            "is anything coming through", "check the signal", "signal present", "is it playing",
            "nothing on the meter",
        ),
        catalog=(
            "output_meter_level", "input_meter_level", "meter", "signal", "level", "normalised",
        ),
        anchors=(
            "track.output_meter_level", "track.input_meter_level", "master.output_meter_level",
            "return.output_meter_level", "track.output_meter_left",
        ),
    ),
    Synonym(
        producer=(
            "width", "stereo width", "widen", "wider", "narrow", "narrower", "mono it",
            "make it mono", "stereo image", "stereo field", "spread", "collapse the stereo",
            "how wide is it",
        ),
        catalog=(
            "panning", "panning_value", "panning_mode", "left_split_stereo",
            "right_split_stereo", "split stereo", "mixer_device", "width",
        ),
        anchors=(
            "track.panning_value", "track.panning_mode", "track.left_split_stereo_value",
            "track.right_split_stereo_value", "chain.panning_value", "track.output_meter_left",
        ),
    ),
    Synonym(
        producer=(
            "balance", "balance it", "place it", "placement", "hard left", "hard right",
            "up the middle", "dead centre", "in the centre", "off to the side",
            "stereo position", "move it left", "move it right",
        ),
        catalog=(
            "panning", "panning_value", "pan", "mixer_device", "value",
        ),
        anchors=(
            "track.panning_value", "return.panning_value", "master.panning_value",
            "chain.panning_value", "drum_pad.chain_panning_value",
        ),
    ),
    Synonym(
        producer=(
            "kill it", "kill the", "cut it", "cut the", "silence it", "drop it out",
            "take it out", "lose the", "shut it off", "pull it out of the mix",
            "get rid of it for now",
        ),
        catalog=(
            "mute", "silent", "silences", "kills", "track_activator", "solo",
        ),
        anchors=(
            "track.mute", "chain.mute", "drum_pad.mute", "return.mute",
            "track.track_activator_value", "master.track_activator_value",
        ),
    ),
    Synonym(
        producer=(
            "turn the channel off", "switch the channel off", "deactivate the track",
            "disable the track", "bypass the channel", "channel on off",
            "take the strip offline", "toggle the track",
        ),
        catalog=(
            "track_activator", "chain_activator", "activator", "switch", "mute", "quantized",
        ),
        anchors=(
            "track.track_activator_value", "chain.chain_activator_value",
            "master.track_activator_value",
        ),
    ),
    Synonym(
        producer=(
            "isolate", "isolate it", "listen alone", "just that one", "audition alone",
            "only hear", "pfl", "afl", "in isolation", "one at a time", "hear it on its own",
        ),
        catalog=(
            "solo", "soloed", "exclusive_solo", "muted_via_solo", "silencing", "silences",
        ),
        anchors=(
            "track.solo", "chain.solo", "drum_pad.solo", "song.exclusive_solo", "return.solo",
            "track.muted_via_solo",
        ),
    ),
    Synonym(
        producer=(
            "why can't i hear it", "no sound", "nothing coming out", "it's gone silent",
            "where did it go", "this one isn't playing", "missing from the mix",
            "dropped out of the mix",
        ),
        catalog=(
            "muted_via_solo", "silent", "audible", "mute", "solo", "output_meter_level",
        ),
        anchors=(
            "track.muted_via_solo", "chain.muted_via_solo", "track.mute",
            "track.output_meter_level", "track.track_activator_value",
        ),
    ),
    Synonym(
        producer=(
            "headphones", "headphone level", "cue level", "cue mix", "dj output", "pre-listen",
            "prelisten", "preview volume", "booth level", "what i hear in the cans",
        ),
        catalog=(
            "cue_volume", "cue_volume_value", "headphone", "cue", "master_track",
            "mixer_device",
        ),
        anchors=(
            "master.cue_volume_value",
        ),
    ),
    Synonym(
        producer=(
            "bus", "sub group", "subgroup", "drum bus", "vocal bus", "group bus", "stem",
            "stems", "route it to the group", "send it to a bus", "print to a bus",
            "fold the group",
        ),
        catalog=(
            "group_track", "is_grouped", "is_foldable", "fold_state", "output_routing_type",
            "group", "main", "output_routing",
        ),
        anchors=(
            "track.output_routing_type", "track.group_track", "track.is_foldable",
            "track.is_grouped", "track.fold_state", "track.output_routing",
        ),
    ),
    Synonym(
        producer=(
            "channel strip", "mixer strip", "the strip", "mixer channel", "channel",
            "strip settings", "whole channel", "copy the channel settings",
        ),
        catalog=(
            "mixer_device", "mixer", "volume", "panning", "track_activator", "sends",
        ),
        anchors=(
            "track.mixer_device", "return.mixer_device", "master.mixer_device",
            "chain.mixer_device",
        ),
    ),
    Synonym(
        producer=(
            "aux", "auxiliary", "fx send", "effects send", "throw it to the reverb",
            "more verb", "more reverb on it", "wetter", "more delay on the vocal",
            "feed the reverb", "how much is going to the",
        ),
        catalog=(
            "sends", "send_value", "send", "mixer_device", "return_tracks", "db",
        ),
        anchors=(
            "track.send_value", "return.send_value", "chain.send_value",
            "drum_pad.chain_send_value", "track.sends", "track.send_name",
        ),
    ),
    Synonym(
        producer=(
            "fx track", "effect track", "effect return", "reverb channel", "delay channel",
            "aux track", "send track", "the reverb bus", "my reverb", "turn the reverb down",
        ),
        catalog=(
            "return_tracks", "return", "sends", "volume_value", "mute", "send_name",
        ),
        anchors=(
            "return.volume_value", "return.mute", "return.name", "return.solo",
            "track.send_value", "track.send_name",
        ),
    ),
    Synonym(
        producer=(
            "2-bus", "two bus", "mix bus", "main out", "main fader", "main output",
            "stereo bus", "final output", "the whole mix", "overall level", "the mixdown level",
        ),
        catalog=(
            "master_track", "master", "volume_value", "mix", "bus", "output_meter_level",
        ),
        anchors=(
            "master.volume_value", "master.output_meter_level", "master.track_activator_value",
            "master.devices",
        ),
    ),
    Synonym(
        producer=(
            "pre-fader", "prefader", "post-fader", "post fader", "pre fx", "post fx",
            "tap point", "where the send is taken from", "before the fader", "after the fader",
        ),
        catalog=(
            "output_routing_channel", "post fx", "pre fx", "post mixer", "routing", "pre-fader",
            "input_routing_channel",
        ),
        anchors=(
            "track.output_routing_channel", "track.available_output_routing_channels",
            "track.input_routing_channel", "return.output_meter_level",
        ),
    ),
    Synonym(
        producer=(
            "xfade", "x-fader", "a/b", "a-b blend", "blend a and b", "dj blend", "deck a",
            "deck b", "assign to the a side", "cut between decks",
        ),
        catalog=(
            "crossfader", "crossfader_value", "crossfade_assign", "crossfader.value",
            "master_track",
        ),
        anchors=(
            "master.crossfader_value", "track.crossfade_assign", "return.crossfade_assign",
        ),
    ),
    Synonym(
        producer=(
            "ride the fader", "volume ride", "fade in", "fade out", "automate the level",
            "draw the level", "level curve", "write a fade", "fade it up over the section",
        ),
        catalog=(
            "volume", "automation_state", "automation_write", "parameter", "fader",
        ),
        anchors=(
            "track.volume_value", "track.volume_automation_state", "master.volume_value",
            "chain.volume_value", "track.volume_re_enable_automation",
        ),
    ),
    Synonym(
        producer=(
            "kit balance", "drum balance", "balance the kit", "internal drum mix",
            "level inside the rack", "too loud in the kit", "one hit is too loud",
            "sample level in the rack", "pad is buried",
        ),
        catalog=(
            "drum_pad", "chain_volume", "chain_volume_value", "mixer_device", "balance", "kit",
            "kick", "snare",
        ),
        anchors=(
            "drum_pad.chain_volume_value", "drum_pad.chain_panning_value", "drum_pad.mute",
            "drum_pad.solo",
        ),
    ),
    Synonym(
        producer=(
            "duck", "ducking", "duck the bass", "pump", "pumping", "breathe with the kick",
            "trigger from the kick", "key it from", "key input", "make it move with the kick",
        ),
        catalog=(
            "input_routing_type", "sidechain", "trigger", "compressor", "audio from", "post fx",
        ),
        anchors=(
            "device.input_routing_type", "device.input_routing_type_name",
            "device.input_routing_types", "track.output_routing_channel", "track.mute",
        ),
    ),
    Synonym(
        producer=(
            "hear the input", "listen to the live input", "input listen", "monitor in",
            "monitor off", "direct monitoring", "why can't i hear what i'm playing",
            "live input through the channel",
        ),
        catalog=(
            "current_monitoring_state", "monitoring", "input", "in", "auto", "off",
            "can_be_armed",
        ),
        anchors=(
            "track.current_monitoring_state", "track.arm", "track.can_be_armed",
        ),
    ),
    # -- routing ---------------------------------------------------------------
    Synonym(
        producer=(
            "duck", "ducking", "duck the bass", "pump", "pumping", "sidechain compression",
            "sidechained", "breathing",
        ),
        catalog=(
            "sidechain", "input_routing_type", "input source", "compressor", "trigger",
            "available_input_routing_types",
        ),
        anchors=(
            "device.input_routing_type", "device.input_routing_types",
            "device.input_routing_channel", "device.input_routing_type_name",
        ),
    ),
    Synonym(
        producer=(
            "key input", "key source", "key it from", "trigger from", "triggered by",
            "sc input", "s/c", "sidechain input", "what triggers the compressor",
        ),
        catalog=(
            "input_routing_type", "input source", "available_input_routing_types", "sidechain",
            "routingtype", "display_name",
        ),
        anchors=(
            "device.input_routing_type", "device.input_routing_types",
            "device.input_routing_type_name",
        ),
    ),
    Synonym(
        producer=(
            "pre fader", "pre-fader", "post fader", "post-fader", "before the fader",
            "after the fader", "pickup point", "where it listens",
        ),
        catalog=(
            "tap point", "post fx", "pre fx", "post mixer", "input_routing_channel",
            "available_input_routing_channels", "sidechain",
        ),
        anchors=(
            "device.input_routing_channel", "device.input_routing_channel_name",
            "device.input_routing_channels", "track.output_routing_channel",
        ),
    ),
    Synonym(
        producer=(
            "bus", "bus it", "bus the drums", "submix", "sub mix", "stems", "route it to a bus",
            "drum bus",
        ),
        catalog=(
            "output destination", "output_routing_type", "available_output_routing_types",
            "group", "main", "routing",
        ),
        anchors=(
            "track.output_routing_type", "track.available_output_routing_types",
            "track.output_routing_channel", "track.output_sub_routing",
        ),
    ),
    Synonym(
        producer=(
            "audio from", "audio to", "midi from", "midi to", "input chooser", "output chooser",
            "in dropdown", "out dropdown",
        ),
        catalog=(
            "input_routing_type", "output_routing_type", "input source", "output destination",
            "routing dropdown", "routing",
        ),
        anchors=(
            "track.input_routing_type", "track.output_routing_type",
            "track.input_routing_channel", "track.output_routing_channel",
            "track.available_input_routing_types", "track.available_output_routing_types",
        ),
    ),
    Synonym(
        producer=(
            "resample", "resampling", "record the master", "record the mix", "bounce in place",
            "capture the output", "record what i hear",
        ),
        catalog=(
            "input_routing_type", "input source", "available_input_routing_types",
            "takes its input from", "input routing", "arm",
        ),
        anchors=(
            "track.input_routing_type", "track.available_input_routing_types",
            "track.input_routing_channel", "track.available_input_routing_channels",
            "track.arm",
        ),
    ),
    Synonym(
        producer=(
            "outboard", "hardware synth", "external instrument", "external gear", "analog gear",
            "out of the box", "hardware insert", "patch to my interface",
        ),
        catalog=(
            "hardware", "physical output", "audio interface", "output destination",
            "output_routing_type", "available_output_routing_types",
        ),
        anchors=(
            "master.output_routing_type", "master.available_output_routing_types",
            "track.output_routing_type", "track.available_output_routing_types",
            "master.output_sub_routing",
        ),
    ),
    Synonym(
        producer=(
            "hear the input", "hear myself", "hear what i am playing", "listen through", "thru",
            "direct monitor", "am i hearing the mic",
        ),
        catalog=(
            "monitoring", "current_monitoring_state", "input monitoring", "can_be_armed",
            "armed",
        ),
        anchors=(
            "track.current_monitoring_state", "track.can_be_armed", "track.arm",
        ),
    ),
    Synonym(
        producer=(
            "aux", "aux send", "auxiliary", "fx send", "effects send", "throw it to the reverb",
            "more verb", "wet amount",
        ),
        catalog=(
            "sends", "send_value", "mixer_device.sends", "return track", "return_tracks",
            "send curve",
        ),
        anchors=(
            "track.send_value", "track.sends", "track.send_name", "return.send_value",
        ),
    ),
    Synonym(
        producer=(
            "feed it", "wire it to", "patch it into", "goes into", "run it into", "point it at",
            "hook it up to", "chain it into",
        ),
        catalog=(
            "output_routing_type", "input_routing_type", "output destination", "input source",
            "available_output_routing_types", "routing",
        ),
        anchors=(
            "track.output_routing_type", "track.input_routing_type",
            "track.available_output_routing_types", "track.available_input_routing_types",
            "device.input_routing_type",
        ),
    ),
    Synonym(
        producer=(
            "unrouted", "disconnect the output", "cut the output", "route to nothing",
            "no destination", "nothing coming in", "kill the routing",
        ),
        catalog=(
            "no input", "sends only", "output destination", "output_routing_type",
            "display_name", "available_input_routing_types",
        ),
        anchors=(
            "track.output_routing_type", "device.input_routing_type_name",
            "device.input_routing_types", "track.available_output_routing_types",
        ),
    ),
    Synonym(
        producer=(
            "2-bus", "two-bus", "stereo out", "mix bus", "master bus", "main outs",
            "monitor outs", "speaker output",
        ),
        catalog=(
            "master_track", "physical output", "hardware", "output_routing_type", "main",
            "master",
        ),
        anchors=(
            "master.output_routing_type", "master.output_sub_routing",
            "master.available_output_routing_types", "track.output_routing_type",
        ),
    ),
    Synonym(
        producer=(
            "ghost kick", "dummy kick", "silent trigger track", "hidden kick",
            "why is it still ducking", "phantom kick", "mute does not stop the sidechain",
            "muted source track",
        ),
        catalog=(
            "trigger track", "post fx", "sidechain", "mute", "input_routing_channel",
            "output_routing_channel",
        ),
        anchors=(
            "track.mute", "track.output_routing_channel", "device.input_routing_channel",
            "device.input_routing_channel_name",
        ),
    ),
    Synonym(
        producer=(
            "prelisten", "pre-listen", "pfl", "afl", "booth output", "phones level",
            "dj monitor level",
        ),
        catalog=(
            "cue_volume", "headphone", "dj output", "cue", "master_track", "mixer_device",
        ),
        anchors=(
            "master.cue_volume_value",
        ),
    ),
    Synonym(
        producer=(
            "x-fader", "xfader", "cross fade", "a/b assign", "assign to the a side", "dj fader",
            "blend a and b",
        ),
        catalog=(
            "crossfade_assign", "crossfader", "crossfader assignment", "mixer_device",
        ),
        anchors=(
            "track.crossfade_assign", "master.crossfader_value", "return.crossfade_assign",
        ),
    ),
    Synonym(
        producer=(
            "glue the drums", "glue them together", "group the drums", "bus them together",
            "sum them", "fold them into one", "make a group",
        ),
        catalog=(
            "group_track", "is_grouped", "grouping", "output_routing_type",
            "output destination", "group",
        ),
        anchors=(
            "track.group_track", "track.is_grouped", "track.output_routing_type",
            "track.available_output_routing_types",
        ),
    ),
    Synonym(
        producer=(
            "channel pair", "input 1 and 2", "mono input", "stereo pair", "which input jack",
            "mic input", "input 3",
        ),
        catalog=(
            "input channel", "1/2", "sub_routing", "input_routing_channel",
            "available_input_routing_channels", "channels",
        ),
        anchors=(
            "track.input_sub_routing", "track.input_routing_channel",
            "track.available_input_routing_channels", "track.output_sub_routing",
        ),
    ),
    Synonym(
        producer=(
            "is signal getting through", "is anything coming in", "no sound coming through",
            "is it reaching the track", "signal present", "vu",
        ),
        catalog=(
            "input_meter_level", "output_meter_level", "is anything arriving", "making sound",
            "meter",
        ),
        anchors=(
            "track.input_meter_level", "track.output_meter_level", "track.has_audio_input",
        ),
    ),
    Synonym(
        producer=(
            "layer two synths", "stack the synths", "play one track from another",
            "double the synth", "drive another track", "midi thru",
            "send notes to another track",
        ),
        catalog=(
            "input_routing_type", "input source", "has_midi_output", "routing helper",
            "passes midi on", "available_input_routing_types",
        ),
        anchors=(
            "track.input_routing_type", "track.available_input_routing_types",
            "track.has_midi_output", "track.has_midi_input",
        ),
    ),
    Synonym(
        producer=(
            "new aux", "add an aux", "make a reverb bus", "new fx track", "effect return",
            "add a send track",
        ),
        catalog=(
            "create_return_track", "return track", "return_tracks", "sends",
        ),
        anchors=(
            "song.create_return_track", "song.return_tracks", "track.sends",
        ),
    ),
    Synonym(
        producer=(
            "pad send", "drum rack send", "send inside the rack", "macro send",
            "send from a rack chain", "rack internal send",
        ),
        catalog=(
            "return chains", "chain_send", "rack", "chain", "sends", "mixer_device.sends",
        ),
        anchors=(
            "chain.send_value", "drum_pad.chain_send_value",
        ),
    ),
    Synonym(
        producer=(
            "signal path", "signal flow", "where does this go", "what is this routed to",
            "trace the audio", "follow the signal",
        ),
        catalog=(
            "current_output_routing", "current_input_routing", "output_routing_type",
            "input_routing_type", "display_name",
        ),
        anchors=(
            "track.output_routing", "track.input_routing", "device.input_routing_type_name",
            "track.output_routing_type", "track.input_routing_type",
        ),
    ),
    # -- clips_notes -----------------------------------------------------------
    Synonym(
        producer=(
            "humanize", "humanize it", "make it human", "less robotic", "not so robotic",
            "too stiff", "stiff", "loosen it up", "loosen", "play it in", "natural feel",
        ),
        catalog=(
            "humanisation", "humanised", "human feel", "quantize", "amount", "velocity",
            "random", "deviation", "timing",
        ),
        anchors=(
            "clip.quantize", "clip.velocity_amount", "groove.random_amount",
            "groove.timing_amount", "clip.get_notes_extended",
        ),
    ),
    Synonym(
        producer=(
            "tighten", "tighten it up", "tight", "make it tight", "lock to the grid",
            "line it up", "clean up the timing", "fix the timing",
        ),
        catalog=(
            "quantize", "quantization_grid", "grid", "snaps hard", "amount", "tighten",
        ),
        anchors=(
            "clip.quantize", "clip.quantize_pitch", "arrangement_clip.quantize",
            "song.midi_recording_quantization",
        ),
    ),
    Synonym(
        producer=(
            "swing", "shuffle", "swung", "add swing", "bounce", "make it bounce", "laid back",
            "behind the beat", "push and pull",
        ),
        catalog=(
            "groove", "timing_amount", "groove pool", "base", "quantization_amount", "swing",
            "1/16 triplet", "feel",
        ),
        anchors=(
            "groove.timing_amount", "groove.base", "groove.clip_assignment", "clip.groove",
            "groove.quantization_amount", "song.swing_amount",
        ),
    ),
    Synonym(
        producer=(
            "ghost note", "ghost notes", "ghosts", "soft hit", "light hit", "barely there",
            "quieter notes", "low velocity",
        ),
        catalog=(
            "velocity", "velocity_deviation", "velocity_amount", "notes", "accent",
            "probability",
        ),
        anchors=(
            "clip.get_notes_extended", "clip.velocity_amount", "groove.velocity_amount",
        ),
    ),
    Synonym(
        producer=(
            "accent", "accent the downbeat", "hit harder", "harder", "dynamics", "more dynamic",
            "punchier",
        ),
        catalog=(
            "velocities", "accent", "velocity_amount", "velocity", "notes",
        ),
        anchors=(
            "groove.velocity_amount", "clip.velocity_amount", "clip.get_notes_extended",
        ),
    ),
    Synonym(
        producer=(
            "nudge", "nudge the notes", "nudge it back", "shift the notes", "move it earlier",
            "move it later", "slide the notes", "micro-timing", "push it late",
        ),
        catalog=(
            "notes", "notes_get", "notes_set", "note id", "from_time", "time_span",
            "clip-local beats", "read-modify-write",
        ),
        anchors=(
            "clip.get_notes_extended", "clip.duplicate_notes_by_id", "clip.position",
        ),
    ),
    Synonym(
        producer=(
            "octave up", "octave down", "up an octave", "drop it an octave",
            "transpose the midi", "move it up 12", "key change",
        ),
        catalog=(
            "transposition_amount", "transpose", "semitones", "pitch", "notes",
            "duplicate_notes_by_id", "pitch_coarse",
        ),
        anchors=(
            "clip.duplicate_notes_by_id", "clip.duplicate_region",
            "clip.get_selected_notes_extended", "clip.pitch_coarse",
        ),
    ),
    Synonym(
        producer=(
            "tune it", "out of tune", "in tune", "tuning", "sharpen it", "flatten it",
            "pitch the sample up",
        ),
        catalog=(
            "pitch_fine", "pitch_coarse", "detune", "cents", "semitones", "re-pitch",
            "warp_mode",
        ),
        anchors=(
            "clip.pitch_fine", "clip.pitch_coarse", "clip.warp_mode",
            "arrangement_clip.pitch_fine",
        ),
    ),
    Synonym(
        producer=(
            "chop", "chop it up", "chop the loop", "cut it up", "cut up", "rearrange the bars",
            "stutter",
        ),
        catalog=(
            "duplicate_region", "beat range", "region", "crop", "loop brace",
            "destination_time", "loop_start", "loop_end",
        ),
        anchors=(
            "clip.duplicate_region", "clip.crop", "clip.loop_start", "clip.loop_end",
            "clip.duplicate_notes_by_id",
        ),
    ),
    Synonym(
        producer=(
            "chop to pads", "chop the break", "break chopping", "chopped sample",
        ),
        catalog=(
            "slicing", "slices", "slice", "slicing_style", "transient", "pads", "sample",
        ),
        anchors=(
            "simpler.sample_slices", "simpler.pad_slicing", "simpler.slicing_style",
            "simpler.sample_slicing_beat_division", "sample.insert_slice",
        ),
    ),
    Synonym(
        producer=(
            "double the length", "double it", "double it up", "extend it", "extend to 8 bars",
            "make it longer", "lengthen", "grow it", "turn 4 bars into 8",
        ),
        catalog=(
            "duplicate_loop", "double the loop length", "grow", "loop_end", "length", "beats",
            "bars",
        ),
        anchors=(
            "clip.duplicate_loop", "clip.loop_end", "clip.length", "clip.end_marker",
        ),
    ),
    Synonym(
        producer=(
            "riff", "lick", "phrase", "motif", "the idea", "the part", "melody line", "topline",
            "the pattern",
        ),
        catalog=(
            "notes", "melody", "midi notes", "notes_get", "clip",
        ),
        anchors=(
            "clip.get_notes_extended", "clip.is_midi_clip", "clip_slot.create_clip",
        ),
    ),
    Synonym(
        producer=(
            "take", "another take", "best take", "take lane", "comp the takes", "comping",
        ),
        catalog=(
            "take_lane", "is_take_lane_clip", "overdubbing", "recording",
        ),
        anchors=(
            "clip.is_take_lane_clip", "arrangement_clip.is_take_lane_clip", "clip.is_recording",
            "clip.is_overdubbing",
        ),
    ),
    Synonym(
        producer=(
            "staccato", "shorten the notes", "shorter notes", "note length", "note duration",
            "choppy", "stabby", "gate the notes",
        ),
        catalog=(
            "notes", "notes_set", "notes_get", "note id", "apply_note_modifications",
            "read-modify-write",
        ),
        anchors=(
            "clip.get_notes_extended", "clip.get_notes_by_id", "clip.remove_notes_by_id",
        ),
    ),
    Synonym(
        producer=(
            "legato", "tie the notes", "connect the notes", "join the notes", "smooth the line",
            "no gaps between notes",
        ),
        catalog=(
            "notes", "notes_set", "note id", "apply_note_modifications", "legato", "launch",
        ),
        anchors=(
            "clip.get_notes_extended", "clip.legato",
        ),
    ),
    Synonym(
        producer=(
            "grid size", "set the grid", "1/16 grid", "triplet grid", "turn off snap",
            "snap setting",
        ),
        catalog=(
            "grid_quantization", "grid_is_triplet", "grid", "triplet", "snapping", "view",
        ),
        anchors=(
            "clip.view_grid_quantization", "clip.view_grid_is_triplet",
            "arrangement_clip.view_grid_quantization", "clip.quantize",
        ),
    ),
    Synonym(
        producer=(
            "50 percent quantise", "half quantise", "partial quantise", "quantise strength",
            "how hard to quantise", "do not fully quantise",
        ),
        catalog=(
            "amount", "partway", "snaps hard", "quantization_amount", "0.0..1.0",
        ),
        anchors=(
            "clip.quantize", "groove.quantization_amount", "clip.quantize_pitch",
        ),
    ),
    Synonym(
        producer=(
            "chance", "randomise", "random notes", "make it vary", "not every time",
            "sometimes", "varied",
        ),
        catalog=(
            "probability", "random_amount", "velocity_deviation", "random", "notes",
        ),
        anchors=(
            "clip.get_notes_extended", "groove.random_amount",
        ),
    ),
    Synonym(
        producer=(
            "disable the clip", "turn the clip off", "kill the clip", "grey it out",
            "switch the clip off",
        ),
        catalog=(
            "muted", "deactivate", "greyed-out", "mute",
        ),
        anchors=(
            "clip.muted", "arrangement_clip.muted",
        ),
    ),
    Synonym(
        producer=(
            "one shot", "one-shot", "unloop", "turn the loop off", "stop it looping",
            "play it through once",
        ),
        catalog=(
            "looping", "loop brace", "start_marker", "end_marker", "loops",
        ),
        anchors=(
            "clip.looping", "clip.start_marker", "clip.end_marker", "arrangement_clip.looping",
        ),
    ),
    Synonym(
        producer=(
            "quantise just the hats", "only the snare", "just the kick", "one drum at a time",
            "per pitch", "leave the kick alone",
        ),
        catalog=(
            "quantize_pitch", "hi-hat", "kick", "note number", "pitch", "tighten",
        ),
        anchors=(
            "clip.quantize_pitch", "arrangement_clip.quantize_pitch",
        ),
    ),
    Synonym(
        producer=(
            "erase the notes", "wipe the clip", "empty the clip", "get rid of the notes",
            "clear the notes",
        ),
        catalog=(
            "remove_notes_extended", "remove_notes_by_id", "delete every note", "notes",
            "destructive",
        ),
        anchors=(
            "clip.remove_notes_extended", "clip.remove_notes_by_id", "clip_slot.delete_clip",
        ),
    ),
    Synonym(
        producer=(
            "program the drums", "lay down a part", "put notes in", "input the melody",
            "enter notes", "write a part",
        ),
        catalog=(
            "notes_set", "add_new_notes", "set_notes", "append", "additive", "notes",
        ),
        anchors=(
            "clip_slot.create_clip",
        ),
    ),
    # -- arrangement -----------------------------------------------------------
    Synonym(
        producer=(
            "drop", "the drop", "build", "buildup", "build-up", "breakdown", "verse", "chorus",
            "pre-chorus", "hook", "intro", "outro", "bridge", "section", "song section", "part",
            "song form",
        ),
        catalog=(
            "locator", "cue_points", "section", "arrangement", "name", "time",
        ),
        anchors=(
            "cue.name", "song.cue_points", "song.set_or_delete_cue", "cue.time", "cue.jump",
        ),
    ),
    Synonym(
        producer=(
            "marker", "markers", "section marker", "cue point", "cue points", "flag",
            "memory location", "arrangement marker", "chapter marker", "signpost",
        ),
        catalog=(
            "locator", "cue_points", "cue", "jump", "arrangement",
        ),
        anchors=(
            "song.cue_points", "cue.name", "song.set_or_delete_cue", "cue.time", "cue.jump",
            "song.jump_to_next_cue", "song.jump_to_prev_cue",
        ),
    ),
    Synonym(
        producer=(
            "drop a marker", "mark it", "mark the drop", "label the section",
            "name the section", "tag it", "set a marker", "place a marker", "add a locator",
            "annotate the timeline",
        ),
        catalog=(
            "set_or_delete_cue", "locator", "toggle", "playhead", "cue_points", "name",
        ),
        anchors=(
            "song.set_or_delete_cue", "cue.name", "song.current_song_time", "song.cue_points",
        ),
    ),
    Synonym(
        producer=(
            "bar", "bars", "measure", "measures", "bar 33", "at bar 65", "8 bars", "16 bars",
            "32 bars", "four bars in", "downbeat", "bar number",
        ),
        catalog=(
            "beats", "beat", "bar", "signature_numerator", "loop_length", "current_song_time",
            "start_time",
        ),
        anchors=(
            "song.current_song_time", "song.loop_length", "song.loop_start", "cue.time",
            "arrangement_clip.start_time", "song.signature_numerator",
        ),
    ),
    Synonym(
        producer=(
            "cursor", "seek", "seek to", "go to", "jump to", "locate", "locate to",
            "move to bar", "rewind", "fast forward", "skip to", "put the playhead",
            "start from here", "where am i",
        ),
        catalog=(
            "current_song_time", "playhead", "jump_by", "beats", "transport", "position",
        ),
        anchors=(
            "song.current_song_time", "song.jump_by", "song.jump_to_next_cue",
            "song.jump_to_prev_cue", "cue.jump",
        ),
    ),
    Synonym(
        producer=(
            "loop the section", "loop the chorus", "loop region", "loop range", "loop brace",
            "cycle", "cycle range", "cycle mode", "loop end", "loop out point",
            "loop from 33 to 49", "set the loop",
        ),
        catalog=(
            "loop_start", "loop_length", "bracket", "arrangement", "beats", "loop",
        ),
        anchors=(
            "song.loop_start", "song.loop_length", "song.loop",
        ),
    ),
    Synonym(
        producer=(
            "punch in at", "punch out at", "drop in", "auto punch", "punch record",
            "record range", "record from bar 33", "record just the chorus", "cycle record",
            "re-record that bit",
        ),
        catalog=(
            "punch_in", "punch_out", "loop bracket", "record_mode", "arrangement recording",
            "loop_start",
        ),
        anchors=(
            "song.punch_in", "song.punch_out", "song.record_mode", "song.loop_start",
            "song.loop_length",
        ),
    ),
    Synonym(
        producer=(
            "drag it to the arrangement", "drop it into the arrangement",
            "commit to arrangement", "print to the timeline", "lay it out",
            "lay down the arrangement", "build the arrangement", "flesh out the arrangement",
            "move the loop into the song", "place the clip at bar 33",
        ),
        catalog=(
            "duplicate_from_session", "duplicate", "destination_time", "arrangement timeline",
            "session",
        ),
        anchors=(
            "arrangement_clip.duplicate_from_session", "arrangement_clip.start_time",
            "arrangement_clip.list", "arrangement_clip.create_midi",
        ),
    ),
    Synonym(
        producer=(
            "move the clip", "slide it later", "slide it earlier", "shift the section",
            "nudge the clip", "scoot it over", "push it back 8 bars", "pull it forward",
            "reposition the clip",
        ),
        catalog=(
            "start_time", "destination_time", "read-only", "timeline", "position",
            "delete_clip",
        ),
        anchors=(
            "arrangement_clip.start_time", "arrangement_clip.duplicate_from_session",
            "arrangement_clip.delete", "arrangement_clip.position",
        ),
    ),
    Synonym(
        producer=(
            "take it out", "drop out", "drop the drums out", "kill it in the breakdown",
            "silence the section", "pull it out", "leave it out", "strip it back",
            "deactivate the clip",
        ),
        catalog=(
            "muted", "deactivate", "section", "timeline", "per-clip mute",
        ),
        anchors=(
            "arrangement_clip.muted", "arrangement_clip.delete", "track.mute",
        ),
    ),
    Synonym(
        producer=(
            "consolidate", "glue", "glue the clips", "glue it into one", "flatten",
            "freeze and flatten", "bounce in place", "merge the clips", "render it down",
        ),
        catalog=(
            "crop", "loop brace", "is_frozen", "not in the lom", "arrangement_clips",
        ),
        anchors=(
            "arrangement_clip.crop", "track.is_frozen", "arrangement_clip.list",
            "arrangement_clip.duplicate_loop",
        ),
    ),
    Synonym(
        producer=(
            "back to arrangement", "release the override", "override", "session is overriding",
            "stop the session clips taking over", "give control back to the timeline",
            "back to arranger button", "clear the override",
        ),
        catalog=(
            "back_to_arranger", "arranger", "overriding", "arrangement playback",
            "back to arrangement",
        ),
        anchors=(
            "song.back_to_arranger", "track.back_to_arranger", "return.back_to_arranger",
            "master.back_to_arranger",
        ),
    ),
    Synonym(
        producer=(
            "arrangement view", "arrange page", "arrange window", "timeline view",
            "linear view", "switch to arrangement", "open the arrangement",
            "show me the timeline", "flip to arrange",
        ),
        catalog=(
            "arranger", "show_view", "focus_view", "focused_document_view",
            "available_main_views", "view",
        ),
        anchors=(
            "app.show_view", "app.focused_document_view", "app.focus_view",
            "app.available_main_views", "app.is_view_visible",
        ),
    ),
    Synonym(
        producer=(
            "auto scroll", "autoscroll", "auto-scroll", "follow playback", "chase the playhead",
            "keep the screen on the playhead", "scroll with the song", "follow mode",
        ),
        catalog=(
            "follow_song", "scroll", "playhead", "arrangement view", "arranger",
        ),
        anchors=(
            "view.follow_song", "app.scroll_view", "app.zoom_view",
        ),
    ),
    Synonym(
        producer=(
            "how long is the track", "how long is it", "duration", "runtime", "total length",
            "how many bars long", "where does it end", "end of the song", "full length",
        ),
        catalog=(
            "song_length", "last_event_time", "arrangement", "beats", "length",
        ),
        anchors=(
            "song.song_length", "song.last_event_time", "arrangement_clip.end_time",
        ),
    ),
    Synonym(
        producer=(
            "time sig", "time sig change", "meter", "odd meter", "change the meter", "in 3",
            "in 6/8", "waltz time", "what's it in",
        ),
        catalog=(
            "signature_numerator", "signature_denominator", "time signature", "beats", "bar",
        ),
        anchors=(
            "song.signature_numerator", "song.signature_denominator",
            "scene.time_signature_numerator", "scene.time_signature_denominator",
            "clip.signature_numerator",
        ),
    ),
    Synonym(
        producer=(
            "pre-roll", "preroll", "count off", "count me in", "lead in", "one two three four",
            "click count", "four clicks before",
        ),
        catalog=(
            "count_in_duration", "is_counting_in", "count-in", "metronome", "recording",
        ),
        anchors=(
            "song.count_in_duration", "song.is_counting_in", "song.metronome",
        ),
    ),
    Synonym(
        producer=(
            "comping", "comp the takes", "another take", "take lanes", "layer another pass",
            "do another pass", "overdub into the arrangement", "keep the old notes",
            "don't erase what's there",
        ),
        catalog=(
            "arrangement_overdub", "overdub", "take_lanes", "recorded notes", "replaces",
        ),
        anchors=(
            "song.arrangement_overdub", "track.take_lanes", "song.record_mode", "song.overdub",
        ),
    ),
    Synonym(
        producer=(
            "next section", "skip ahead", "skip to the chorus", "jump to the drop",
            "previous section", "go back a section", "last section", "next marker",
            "step through the sections",
        ),
        catalog=(
            "jump_to_next_cue", "jump_to_prev_cue", "locator", "playhead", "next", "previous",
        ),
        anchors=(
            "song.jump_to_next_cue", "song.jump_to_prev_cue", "song.can_jump_to_next_cue",
            "song.can_jump_to_prev_cue", "cue.jump",
        ),
    ),
    Synonym(
        producer=(
            "block out", "block out the song", "sketch the arrangement", "map out the song",
            "draw a clip on the timeline", "empty clip in the arrangement", "lay out 8 bars",
            "placeholder", "rough out the structure",
        ),
        catalog=(
            "create_midi", "empty midi clip", "arrangement timeline", "beat positions",
            "arrangement_clips",
        ),
        anchors=(
            "arrangement_clip.create_midi", "arrangement_clip.duplicate_from_session",
            "arrangement_clip.list",
        ),
    ),
    Synonym(
        producer=(
            "chorus scene", "verse scene", "section row", "name the scenes after sections",
            "song sections in session", "arrange with scenes", "one scene per section",
            "structure in session view",
        ),
        catalog=(
            "scene", "song sections", "name", "create_scene", "capture_and_insert_scene",
        ),
        anchors=(
            "scene.name", "song.create_scene", "song.capture_and_insert_scene", "song.scenes",
            "song.duplicate_scene",
        ),
    ),
    Synonym(
        producer=(
            "play from here", "play from the playhead", "audition the section",
            "preview the range", "play just the chorus", "take it from the top",
            "run it from the drop", "hear that bit",
        ),
        catalog=(
            "play_selection", "arrangement selection", "continue_playing", "start marker",
            "playhead",
        ),
        anchors=(
            "song.play_selection", "song.continue_playing", "song.start_playing",
            "song.current_song_time",
        ),
    ),
    Synonym(
        producer=(
            "automate the filter over the drop", "sweep across the build",
            "automation in the arrangement", "draw automation on the timeline",
            "ride the fader through the chorus", "automate the section",
            "move the cutoff over 16 bars",
        ),
        catalog=(
            "automation_envelope", "arrangement clips", "none", "duplicate", "envelope",
        ),
        anchors=(
            "arrangement_clip.automation_envelope", "arrangement_clip.duplicate_from_session",
            "clip.automation_envelope", "clip.create_automation_envelope",
        ),
    ),
    Synonym(
        producer=(
            "copy the verse to make the chorus", "duplicate the section",
            "same section with a different filter", "variation on the section",
            "reuse the loop but change the automation", "another 8 bars but different",
        ),
        catalog=(
            "sections", "duplicate_clip_to", "duplicate_clip_slot", "envelopes", "clip slot",
            "automation",
        ),
        anchors=(
            "clip_slot.duplicate_clip_to", "track.duplicate_clip_slot", "song.duplicate_scene",
            "arrangement_clip.duplicate_from_session",
        ),
    ),
    # -- devices ---------------------------------------------------------------
    Synonym(
        producer=(
            "bypass", "bypassed", "deactivate", "kill the effect", "power off", "unplug",
            "take it out of the chain",
        ),
        catalog=(
            "device.on", "is_active", "parameters[0]", "quantized", "switch",
        ),
        anchors=(
            "device.on_value", "device.is_active", "chain.device_on_value",
            "drum_pad.device_on_value", "return_device.on_value", "master_device.on_value",
        ),
    ),
    Synonym(
        producer=(
            "duck", "ducking", "pump", "pumping", "key input", "breathe",
            "trigger it off the kick",
        ),
        catalog=(
            "sidechain", "input_routing_type", "trigger", "audio from", "compressor",
        ),
        anchors=(
            "device.input_routing_type", "device.input_routing_types",
            "device.input_routing_type_name", "device.input_routing_channel",
            "device.input_routing_channels", "device.input_routing_channel_name",
        ),
    ),
    Synonym(
        producer=(
            "patch", "patches", "init patch", "factory patch", "program",
        ),
        catalog=(
            "preset", "instruments", "browser", "load_item", "sounds", "uri",
        ),
        anchors=(
            "browser.sounds", "browser.instruments", "browser.load_item", "browser.item_name",
            "browser.item_uri", "browser.hotswap_target",
        ),
    ),
    Synonym(
        producer=(
            "layer", "layers", "branch", "branches", "stacked sound",
        ),
        catalog=(
            "chains", "chain", "rack", "chain_activator", "mute", "solo",
        ),
        anchors=(
            "rack.chains", "chain.name", "chain.mute", "chain.solo",
            "chain.chain_activator_value", "chain.volume_value", "rack.view.selected_chain",
        ),
    ),
    Synonym(
        producer=(
            "clap", "toms", "floor tom", "rimshot", "ride", "cowbell", "shaker", "perc",
        ),
        catalog=(
            "drum_pads", "pad", "kit", "kick", "snare", "chains",
        ),
        anchors=(
            "rack.drum_pads", "drum_pad.mute", "drum_pad.solo", "drum_pad.chain_volume_value",
            "drum_pad.chain_panning_value", "drum_pad.chains", "drum_pad.chain_name",
            "rack.view.selected_drum_pad",
        ),
    ),
    Synonym(
        producer=(
            "tune", "retune", "tuning", "root note", "pitch it up",
        ),
        catalog=(
            "out_note", "note", "transposes", "midi note", "pitched sample",
        ),
        anchors=(
            "drum_pad.chain_out_note", "chain.out_note", "drum_pad.note", "rack_drum_pad.note",
        ),
    ),
    Synonym(
        producer=(
            "chop", "chopped", "chop up", "cut it up", "dice",
        ),
        catalog=(
            "slicing", "slices", "playback_mode", "transient", "insert_slice",
        ),
        anchors=(
            "simpler.playback_mode", "simpler.sample_slices", "sample.insert_slice",
            "sample.clear_slices", "simpler.sample_slicing_style", "simpler.pad_slicing",
            "simpler.sample_slicing_beat_division",
        ),
    ),
    Synonym(
        producer=(
            "glue", "glue them together", "wrap in a rack", "bundle", "combine into one device",
        ),
        catalog=(
            "can_have_chains", "rack", "chains", "instrumentgroup", "drumgroupdevice",
            "can_show_chains",
        ),
        anchors=(
            "device.can_have_chains", "rack.chains", "device.class_name",
            "device.can_show_chains", "device.is_showing_chains", "track.can_show_chains",
        ),
    ),
    Synonym(
        producer=(
            "dead knob", "no effect when i turn it", "unmapped", "greyed out", "not hooked up",
            "locked",
        ),
        catalog=(
            "has_macro_mappings", "macros_mapped", "inert", "is_enabled", "disabled",
            "irrelevant",
        ),
        anchors=(
            "rack.has_macro_mappings", "rack.macros_mapped", "param.is_enabled", "param.state",
        ),
    ),
    Synonym(
        producer=(
            "morph", "morphing", "macro preset", "save the knob positions",
            "restore the knob positions",
        ),
        catalog=(
            "variation", "store_variation", "recall", "macro positions", "snapshots",
        ),
        anchors=(
            "rack.variation_count", "rack.store_variation", "rack.recall_selected_variation",
            "rack.recall_last_used_variation", "rack.selected_variation_index",
            "rack.delete_selected_variation",
        ),
    ),
    Synonym(
        producer=(
            "glide", "portamento", "legato", "voice stealing", "one note at a time",
        ),
        catalog=(
            "voices", "polyphony", "monophonic", "mono_poly", "poly_voices", "retrigger",
        ),
        anchors=(
            "simpler.voices", "wavetable.mono_poly", "wavetable.poly_voices",
            "simpler.retrigger",
        ),
    ),
    Synonym(
        producer=(
            "fatten", "thicken", "wider", "supersaw", "huge",
        ),
        catalog=(
            "unison", "unison_mode", "unison_voice_count", "voices",
        ),
        anchors=(
            "wavetable.unison_mode", "wavetable.unison_voice_count", "wavetable.poly_voices",
        ),
    ),
    Synonym(
        producer=(
            "convolution", "hall", "cathedral", "plate", "room sound",
        ),
        catalog=(
            "ir_category_list", "ir_file_list", "ir_file_index", "impulse-response",
            "impulse responses", "hybridreverbdevice",
        ),
        anchors=(
            "hybrid_reverb.ir_category_list", "hybrid_reverb.ir_file_list",
            "hybrid_reverb.ir_category_index", "hybrid_reverb.ir_file_index",
            "hybrid_reverb.ir_size_factor", "hybrid_reverb.ir_decay_time",
        ),
    ),
    Synonym(
        producer=(
            "pdc", "plugin delay compensation", "delay compensation", "lag", "timing drift",
        ),
        catalog=(
            "latency_in_ms", "latency_in_samples", "latency",
        ),
        anchors=(
            "device.latency_in_ms", "device.latency_in_samples", "return_device.latency_in_ms",
            "return_device.latency_in_samples", "chain.latency_in_ms",
            "chain.latency_in_samples", "master_device.latency_in_ms",
            "master_device.latency_in_samples",
        ),
    ),
    Synonym(
        producer=(
            "rearrange", "chain order", "signal flow", "put it first", "move it to the front",
            "swap the order",
        ),
        catalog=(
            "move_device", "signal order", "appended", "device_insert_mode", "position",
        ),
        anchors=(
            "song.move_device", "device.list", "track.view_device_insert_mode", "chain.devices",
            "track.devices",
        ),
    ),
    Synonym(
        producer=(
            "synth", "synths", "synthesizer", "outboard", "which plugins are loaded",
        ),
        catalog=(
            "class_name", "class_display_name", "devices", "instrument", "plugindevice",
        ),
        anchors=(
            "device.list", "device.class_name", "device.class_display_name", "device.name",
            "device.type", "track.devices",
        ),
    ),
    Synonym(
        producer=(
            "nothing plays", "not making noise", "why is it quiet", "dead rack", "no audio",
        ),
        catalog=(
            "silent", "chains", "has_drum_pads", "empty", "makes no sound",
        ),
        anchors=(
            "rack.chains", "rack.has_drum_pads", "drum_pad.chains", "chain.devices",
            "drum_pad.chain_devices",
        ),
    ),
    Synonym(
        producer=(
            "aux", "aux bus", "bus", "shared fx", "group reverb",
        ),
        catalog=(
            "return_chains", "return chain", "sends", "reverb", "rack_return",
        ),
        anchors=(
            "rack.return_chains", "rack_return.devices", "rack_return.name",
            "rack_return.volume_value", "chain.send_value", "drum_pad.chain_send_value",
        ),
    ),
    Synonym(
        producer=(
            "m/s", "ms eq", "side only", "mid only", "elliptical",
        ),
        catalog=(
            "global_mode", "mid_side", "left_right", "stereo", "eq eight", "edit_mode",
        ),
        anchors=(
            "eq8.global_mode", "eq8.edit_mode", "master_device.global_mode",
            "master_device.edit_mode", "device.selected_band",
        ),
    ),
    Synonym(
        producer=(
            "dropdown", "menu", "options", "choices", "what settings does it have",
        ),
        catalog=(
            "value_items", "quantized", "steps", "labels", "str_for_value",
        ),
        anchors=(
            "param.value_items", "param.is_quantized", "param.short_value_items",
            "param.str_for_value", "param.name",
        ),
    ),
    Synonym(
        producer=(
            "expand the rack", "unhide", "see inside the rack", "open it up",
        ),
        catalog=(
            "is_showing_chains", "can_show_chains", "unfolded", "sub-tracks", "chains",
        ),
        anchors=(
            "track.can_show_chains", "track.is_showing_chains", "device.can_show_chains",
            "device.is_showing_chains", "device.is_showing_chain_devices",
        ),
    ),
    Synonym(
        producer=(
            "too loud", "quieter", "louder", "turn it down", "balance the kit",
        ),
        catalog=(
            "chain_volume", "mixer_device.volume", "volume", "sample_gain", "level", "db",
        ),
        anchors=(
            "drum_pad.chain_volume_value", "simpler.pad_sample_gain", "chain.volume_value",
            "rack_return.volume_value", "simpler.sample_gain",
        ),
    ),
    Synonym(
        producer=(
            "which wav", "wav file", "missing sample", "relink", "offline",
        ),
        catalog=(
            "sample_file_path", "file_path", "audio file", "sample", "originalsimpler",
        ),
        anchors=(
            "simpler.sample_file_path", "simpler.pad_sample_file_path",
            "drum_pad.device_class_name", "simpler.sample",
        ),
    ),
    Synonym(
        producer=(
            "exclusive group", "mute group", "cut off the previous hit",
            "hats cutting each other", "hihat group",
        ),
        catalog=(
            "choke_group", "choke", "open-hat", "closed-hat", "drum rack chains",
        ),
        anchors=(
            "chain.choke_group", "drum_pad.chain_choke_group",
        ),
    ),
    Synonym(
        producer=(
            "top and tail", "tighten", "truncate", "cut the tail", "chop the ends off",
        ),
        catalog=(
            "start_marker", "end_marker", "crop", "trimming", "sample frames",
        ),
        anchors=(
            "simpler.sample_start_marker", "simpler.sample_end_marker",
            "simpler.pad_sample_start_marker", "simpler.pad_sample_end_marker", "simpler.crop",
        ),
    ),
    Synonym(
        producer=(
            "only one knob", "no parameters showing", "cannot see the controls",
            "hidden parameters", "nothing to automate",
        ),
        catalog=(
            "configure", "parameters", "get_parameter_names", "strip", "plugin",
        ),
        anchors=(
            "device.parameters", "device.get_parameter_names", "chain.device_parameters",
            "return_device.parameters", "master_device.parameters",
        ),
    ),
    # -- browser_load ----------------------------------------------------------
    Synonym(
        producer=(
            "drop in", "drop a", "drop it on", "throw on", "throw a", "slap on", "stick a",
            "chuck on", "bang on", "put it in",
        ),
        catalog=(
            "load", "load_item", "browser", "item", "loadable", "selected",
        ),
        anchors=(
            "browser.load_item", "browser.item", "browser.item_is_loadable",
            "view.selected_track_devices",
        ),
    ),
    Synonym(
        producer=(
            "add a reverb", "put a compressor on", "a delay", "an eq", "saturator", "some fx",
            "fx", "add a plugin to the chain", "another processor",
        ),
        catalog=(
            "audio effects", "effect", "appended", "chain", "load", "browser",
        ),
        anchors=(
            "browser.audio_effects", "browser.load_item", "browser.item_is_loadable",
        ),
    ),
    Synonym(
        producer=(
            "synth", "patch", "lead", "pluck", "bass patch", "keys", "poly", "an 808 bass",
        ),
        catalog=(
            "instruments", "sounds", "presets", "instrument", "load", "replaces",
        ),
        anchors=(
            "browser.instruments", "browser.sounds", "browser.load_item",
        ),
    ),
    Synonym(
        producer=(
            "m4l", "max patch", "max msp", "max/msp", "maxforlive",
        ),
        catalog=(
            "max for live", "max devices", "mxdeviceinstrument", "class name",
        ),
        anchors=(
            "browser.max_for_live", "device.class_name",
        ),
    ),
    Synonym(
        producer=(
            "audio unit", "outboard", "3rd party", "non-native", "external synth", "u-he",
            "serum", "kontakt",
        ),
        catalog=(
            "plugins", "vst2", "vst3", "au", "third-party", "device on", "configure",
        ),
        anchors=(
            "browser.plugins", "browser.load_item",
        ),
    ),
    Synonym(
        producer=(
            "one shot", "one-shot", "oneshot", "wav", "break", "vocal chop", "stem",
            "field recording", "acapella",
        ),
        catalog=(
            "samples", "audio files", "raw", "simpler", "warp", "load",
        ),
        anchors=(
            "browser.samples", "browser.load_item", "simpler.sample_file_path",
        ),
    ),
    Synonym(
        producer=(
            "loop", "midi loop", "8 bar loop", "a groove file", "prefab clip",
        ),
        catalog=(
            "clips", "alc", "clip slot", "notes", "envelopes", "load",
        ),
        anchors=(
            "browser.clips", "browser.load_item", "browser.samples",
        ),
    ),
    Synonym(
        producer=(
            "favourites", "favorites", "collections", "starred", "bookmarked", "my go-to",
            "my stuff", "my own",
        ),
        catalog=(
            "user library", "saved", "presets", "racks", "folders", "sidebar", "places",
            "source",
        ),
        anchors=(
            "browser.user_library", "browser.user_folders", "browser.item_source",
        ),
    ),
    Synonym(
        producer=(
            "where does live save", "where is it stored", "on disk", "which directory",
            "file location",
        ),
        catalog=(
            "user library", "library.cfg", "projectpath", "preferences", "folder", "project",
            "file_path",
        ),
        anchors=(
            "browser.user_library", "browser.current_project", "simpler.sample_file_path",
        ),
    ),
    Synonym(
        producer=(
            "expansion", "sound bank", "content pack", "do i own", "on this machine",
            "did it come with",
        ),
        catalog=(
            "packs", "installed", "installation", "uri", "source", "pack name",
        ),
        anchors=(
            "browser.packs", "browser.item_source", "browser.item_uri",
        ),
    ),
    Synonym(
        producer=(
            "replace it", "switch it for", "try a different", "change it to", "sub in",
            "a-b it", "put a different one in",
        ),
        catalog=(
            "hotswap", "hot-swap", "hotswap_target", "filter", "loadable", "browser", "load",
        ),
        anchors=(
            "browser.hotswap_target", "browser.filter_type",
            "browser.relation_to_hotswap_target", "browser.load_item",
        ),
    ),
    Synonym(
        producer=(
            "assign a sample to", "map to a pad", "swap the snare", "change the kick sound",
            "put it on that pad", "aim the load at",
        ),
        catalog=(
            "hotswap", "drum rack", "pad", "drum_pads", "sample", "chains", "file_path",
        ),
        anchors=(
            "browser.hotswap_target", "rack.drum_pads", "simpler.pad_sample_file_path",
            "drum_pad.chains",
        ),
    ),
    Synonym(
        producer=(
            "let me hear", "play me", "listen to", "try it out", "check it first",
            "have a listen",
        ),
        catalog=(
            "preview", "audition", "sample", "browser", "stop_preview", "sound",
        ),
        anchors=(
            "browser.preview_item", "browser.stop_preview",
        ),
    ),
    Synonym(
        producer=(
            "find me a", "look for", "show me", "recommend", "suggest", "something like",
            "dig for", "hunt for", "what have i got",
        ),
        catalog=(
            "name", "search", "matching", "uri", "browser", "children", "entries",
        ),
        anchors=(
            "browser.item_name", "browser.item_uri", "browser.packs", "browser.item_children",
        ),
    ),
    Synonym(
        producer=(
            "add a track", "make me a track", "spin up a track", "another channel",
            "extra track",
        ),
        catalog=(
            "create_midi_track", "create_audio_track", "insert", "midi track", "audio track",
            "load",
        ),
        anchors=(
            "song.create_midi_track", "song.create_audio_track", "browser.load_item",
        ),
    ),
    Synonym(
        producer=(
            "on this track", "on the current track", "where does it land",
            "which track will it go on", "on the one i'm looking at",
        ),
        catalog=(
            "selected", "selection", "track", "load", "browser", "targets", "selected_track",
        ),
        anchors=(
            "view.selected_track_devices", "view.selected_track", "browser.load_item",
        ),
    ),
    Synonym(
        producer=(
            "before the compressor", "in front of", "after the eq", "at the top of the chain",
            "move it up", "first in the chain", "last in the chain",
        ),
        catalog=(
            "insert-mode", "device_insert_mode", "position", "order", "move_device", "appended",
            "left", "right", "chain",
        ),
        anchors=(
            "track.view_device_insert_mode", "song.move_device", "browser.audio_effects",
        ),
    ),
    Synonym(
        producer=(
            "nothing plays", "not making noise", "why is it quiet", "dead track",
            "loaded but nothing happens", "no output",
        ),
        catalog=(
            "silent", "no sound", "empty", "kit", "drum rack", "container", "chains",
            "is_device",
        ),
        anchors=(
            "rack.chains", "browser.drums", "rack.has_drum_pads", "browser.item_is_device",
        ),
    ),
    Synonym(
        producer=(
            "factory", "comes with live", "shipped with", "is it mine", "custom", "vanilla",
        ),
        catalog=(
            "source", "pack name", "user library", "built-in", "content", "uri", "packs",
        ),
        anchors=(
            "browser.item_source", "browser.item_uri", "browser.packs",
        ),
    ),
    Synonym(
        producer=(
            "look inside", "open it up", "drill down", "go deeper", "what's in there",
            "expand it",
        ),
        catalog=(
            "folder", "children", "entries", "loadable", "descending", "is_folder",
        ),
        anchors=(
            "browser.item_is_folder", "browser.item_children", "browser.item_is_loadable",
            "browser.child",
        ),
    ),
    Synonym(
        producer=(
            "load a bunch", "all at once", "bulk", "one after another", "load ten of them",
            "queue up",
        ),
        catalog=(
            "rapid succession", "crash", "batch", "pace", "plugins", "load_item",
        ),
        anchors=(
            "browser.load_item", "browser.plugins",
        ),
    ),
    Synonym(
        producer=(
            "name changed", "why is my track called", "different name now", "title changed",
            "renamed itself",
        ),
        catalog=(
            "renames", "track", "default name", "instrument", "load",
        ),
        anchors=(
            "browser.load_item", "track.name",
        ),
    ),
    Synonym(
        producer=(
            "bring in", "drag in", "off my hard drive", "from my downloads",
            "add a file from disk",
        ),
        catalog=(
            "user folder", "new track", ".mid", "samples", "imported", "load_item",
        ),
        anchors=(
            "browser.load_item", "browser.user_folders", "browser.samples",
            "browser.current_project",
        ),
    ),
    Synonym(
        producer=(
            "what can i load", "what's available", "which categories", "what does my live have",
            "which edition",
        ),
        catalog=(
            "roots", "browser", "editions", "standard", "lists", "runtime",
        ),
        anchors=(
            "browser.root", "browser.instruments", "browser.audio_effects",
        ),
    ),
    # -- transport -------------------------------------------------------------
    Synonym(
        producer=(
            "roll", "rolling", "let it roll", "hit it", "kick it off", "get it going", "run it",
            "play the set",
        ),
        catalog=(
            "start_playing", "continue_playing", "is_playing", "transport", "playing",
        ),
        anchors=(
            "song.start_playing", "song.is_playing", "song.continue_playing",
            "song.stop_playing",
        ),
    ),
    Synonym(
        producer=(
            "from the top", "take it from the top", "back to start", "start over", "rewind",
            "reset the playhead", "run it again",
        ),
        catalog=(
            "start_playing", "start marker", "continue_playing", "current_song_time",
            "playhead", "transport",
        ),
        anchors=(
            "song.start_playing", "song.current_song_time", "song.continue_playing",
            "song.stop_playing",
        ),
    ),
    Synonym(
        producer=(
            "bar", "bars", "at bar 33", "bar 9", "downbeat", "eight bars in", "cursor",
            "play cursor", "seek", "go to bar", "park it at",
        ),
        catalog=(
            "beats", "current_song_time", "playhead", "position", "jump_by", "loop_start",
            "loop_length",
        ),
        anchors=(
            "song.current_song_time", "song.jump_by", "song.loop_start", "song.loop_length",
            "cue.time",
        ),
    ),
    Synonym(
        producer=(
            "jog", "shuttle", "scrub through", "fast forward", "ffwd", "wind forward",
            "drag the playhead",
        ),
        catalog=(
            "scrub", "scrub_by", "jump_by", "current_song_time", "playhead", "beats",
            "stop_scrub",
        ),
        anchors=(
            "song.jump_by", "song.current_song_time", "clip.scrub", "clip.stop_scrub",
        ),
    ),
    Synonym(
        producer=(
            "pre-roll", "preroll", "count off", "count-off", "lead in", "two bar count",
            "four clicks first",
        ),
        catalog=(
            "count_in", "count-in", "is_counting_in", "metronome", "recording",
        ),
        anchors=(
            "song.count_in_duration", "song.is_counting_in", "song.metronome",
        ),
    ),
    Synonym(
        producer=(
            "kill", "kill it", "panic", "all stop", "cut everything", "stop everything",
            "shut it up", "silence the session",
        ),
        catalog=(
            "stop_all_clips", "stop", "clips", "playing_slot_index", "session",
        ),
        anchors=(
            "song.stop_all_clips", "track.stop_all_clips", "clip_slot.stop",
            "song.stop_playing",
        ),
    ),
    Synonym(
        producer=(
            "quantize the launch", "launch quantize", "wait for the bar", "on the one",
            "on the downbeat", "free launch", "fire instantly", "no quantize",
            "snap to the bar",
        ),
        catalog=(
            "clip_trigger_quantization", "launch_quantization", "quantisation", "quantization",
            "q_bar", "q_no_q", "q_global",
        ),
        anchors=(
            "song.clip_trigger_quantization", "clip.launch_quantization",
            "arrangement_clip.launch_quantization", "clip_slot.is_triggered", "clip_slot.fire",
        ),
    ),
    Synonym(
        producer=(
            "input quantize", "auto quantize", "record quantize",
            "snap what i play to the grid", "tighten my playing", "quantize while recording",
        ),
        catalog=(
            "midi_recording_quantization", "quantisation", "quantization", "recording",
            "swing_amount",
        ),
        anchors=(
            "song.midi_recording_quantization", "song.swing_amount", "clip.quantize",
        ),
    ),
    Synonym(
        producer=(
            "shuffle", "shuffled", "humanize", "humanise", "swing feel", "laid back",
            "make it swing", "groove template",
        ),
        catalog=(
            "swing_amount", "groove_amount", "groove", "groove_pool",
        ),
        anchors=(
            "song.swing_amount", "song.groove_amount", "song.groove_pool", "clip.groove",
            "song.groove_pool.grooves",
        ),
    ),
    Synonym(
        producer=(
            "speed", "speed it up", "slow it down", "faster", "slower", "how fast",
            "beats per minute", "half time",
        ),
        catalog=(
            "tempo", "bpm", "tap_tempo", "nudge",
        ),
        anchors=(
            "song.tempo", "song.tap_tempo", "song.nudge_up", "song.nudge_down", "scene.tempo",
        ),
    ),
    Synonym(
        producer=(
            "ritardando", "rit", "accelerando", "gradual tempo change", "speed up over 8 bars",
            "tempo automation",
        ),
        catalog=(
            "tempo", "scene.tempo", "tempo_enabled", "automated", "arrangement",
        ),
        anchors=(
            "song.tempo", "scene.tempo", "scene.tempo_enabled", "song.tempo_follower_enabled",
        ),
    ),
    Synonym(
        producer=(
            "drop in", "drop out", "auto punch", "punch range", "cycle", "cycle region",
            "record only over the loop", "record just that section",
        ),
        catalog=(
            "punch_in", "punch_out", "loop", "bracket", "loop_start", "loop_length",
            "record_mode",
        ),
        anchors=(
            "song.punch_in", "song.punch_out", "song.loop_start", "song.loop_length",
            "song.record_mode",
        ),
    ),
    Synonym(
        producer=(
            "marker", "markers", "drop a marker", "memory location", "chapter marker",
            "jump to the next marker",
        ),
        catalog=(
            "locator", "cue", "cue_points", "set_or_delete_cue", "jump_to_next_cue", "playhead",
        ),
        anchors=(
            "song.set_or_delete_cue", "song.cue_points", "cue.jump", "song.jump_to_next_cue",
            "song.jump_to_prev_cue", "cue.time",
        ),
    ),
    Synonym(
        producer=(
            "retro record", "retrospective record", "retrospective midi",
            "grab what i just played", "i forgot to hit record", "save that idea",
        ),
        catalog=(
            "capture_midi", "can_capture_midi", "capture", "buffer",
        ),
        anchors=(
            "song.capture_midi", "song.can_capture_midi",
        ),
    ),
    Synonym(
        producer=(
            "layer another pass", "stack takes", "add to what's there", "keep the old notes",
            "play on top of it", "comp another take", "don't erase what i played",
        ),
        catalog=(
            "overdub", "arrangement_overdub", "is_overdubbing", "replaces", "recorded notes",
        ),
        anchors=(
            "song.arrangement_overdub", "song.overdub", "clip.is_overdubbing",
            "arrangement_clip.is_overdubbing",
        ),
    ),
    Synonym(
        producer=(
            "flashing", "queued", "it hasn't started yet", "why isn't it playing yet",
            "still waiting", "nothing happened when i fired it",
        ),
        catalog=(
            "is_triggered", "triggered", "fired_slot_index", "playing_slot_index",
            "quantisation", "quantization",
        ),
        anchors=(
            "clip_slot.is_triggered", "track.fired_slot_index", "scene.is_triggered",
            "track.playing_slot_index", "song.session_record_status",
        ),
    ),
    Synonym(
        producer=(
            "clock", "master clock", "external clock", "slave", "sync to another machine",
            "jam with a friend", "wifi sync",
        ),
        catalog=(
            "link", "ableton_link", "sync", "tempo", "follower",
        ),
        anchors=(
            "song.is_ableton_link_enabled", "song.is_ableton_link_start_stop_sync_enabled",
            "song.tempo_follower_enabled",
        ),
    ),
    Synonym(
        producer=(
            "how long is the song", "duration in minutes", "length in seconds",
            "where does it end", "total length",
        ),
        catalog=(
            "song_length", "last_event_time", "beats", "arrangement", "current_song_time",
        ),
        anchors=(
            "song.song_length", "song.last_event_time", "song.current_song_time",
        ),
    ),
    Synonym(
        producer=(
            "hear myself", "hear the input", "can't hear my keyboard", "listen through", "thru",
            "direct monitoring", "live input",
        ),
        catalog=(
            "monitoring", "current_monitoring_state", "arm", "input", "can_be_armed",
        ),
        anchors=(
            "track.current_monitoring_state", "track.arm", "track.can_be_armed",
        ),
    ),
    Synonym(
        producer=(
            "record exactly 8 bars", "fixed length recording", "auto stop the recording",
            "record a set number of bars", "timed recording",
        ),
        catalog=(
            "record_length", "fixed length", "trigger_session_record", "session_record_status",
            "session_record",
        ),
        anchors=(
            "clip_slot.fire", "song.session_record_status", "song.session_record",
        ),
    ),
    Synonym(
        producer=(
            "seamless", "no gap", "without a gap", "switch clips smoothly", "no stutter",
            "keep the position when switching",
        ),
        catalog=(
            "legato", "launch", "playing position", "start marker",
        ),
        anchors=(
            "clip.legato", "arrangement_clip.legato", "clip.launch_mode",
        ),
    ),
    Synonym(
        producer=(
            "one shot", "momentary", "hold to play", "press and hold", "latching launch",
            "hold the pad",
        ),
        catalog=(
            "launch_mode", "gate", "toggle", "repeat", "set_fire_button_state", "held button",
        ),
        anchors=(
            "clip.launch_mode", "clip.set_fire_button_state", "arrangement_clip.launch_mode",
        ),
    ),
    Synonym(
        producer=(
            "audition", "preview this section", "play just this bit", "play from here",
            "spot play",
        ),
        catalog=(
            "play_selection", "selection", "continue_playing", "current_song_time", "playhead",
        ),
        anchors=(
            "song.play_selection", "song.continue_playing", "song.current_song_time",
        ),
    ),
    Synonym(
        producer=(
            "next section", "go to the drop", "breakdown", "chorus", "section change",
            "jump to the next part",
        ),
        catalog=(
            "scene", "scenes", "fire", "fire_as_selected", "is_triggered", "clip_slots",
        ),
        anchors=(
            "scene.fire", "scene.fire_as_selected", "scene.name", "scene.is_triggered",
            "song.capture_and_insert_scene",
        ),
    ),
    # -- diagnostic ------------------------------------------------------------
    Synonym(
        producer=(
            "no sound", "nothing coming out", "cant hear", "can't hear it", "dead track",
            "not making noise", "silence on this track", "why is it silent", "nothing audible",
            "no output", "not sounding",
        ),
        catalog=(
            "output_meter_level", "making sound", "audible", "silent", "muted_via_solo",
            "meter",
        ),
        anchors=(
            "track.output_meter_level", "track.muted_via_solo", "track.track_activator_value",
            "clip.muted", "track.has_audio_output",
        ),
    ),
    Synonym(
        producer=(
            "clipping", "peaking", "in the red", "too hot", "distorting", "overloading",
            "redlining", "hitting the ceiling", "too loud",
        ),
        catalog=(
            "output_meter_level", "output_meter_left", "output_meter_right", "meter",
            "normalised",
        ),
        anchors=(
            "track.output_meter_level", "master.output_meter_level", "track.output_meter_left",
            "track.output_meter_right", "master.output_meter_left",
        ),
    ),
    Synonym(
        producer=(
            "too quiet", "why is it quiet", "barely audible", "low level", "turned down",
            "how loud is it", "gain staging", "level in db",
        ),
        catalog=(
            "volume_value", "volume_display_value", "gain_display_string", "db", "display",
        ),
        anchors=(
            "track.volume_value", "track.volume_display_value", "clip.gain_display_string",
            "clip.gain", "track.output_meter_level",
        ),
    ),
    Synonym(
        producer=(
            "deactivated", "greyed out", "channel is off", "not enabled",
            "turned the track off", "disabled channel", "inactive track",
        ),
        catalog=(
            "track_activator", "activator", "switch", "mixer", "mute",
        ),
        anchors=(
            "track.track_activator_value", "return.track_activator_value",
            "master.track_activator_value", "chain.chain_activator_value",
        ),
    ),
    Synonym(
        producer=(
            "bypassed", "bypass", "plugin is off", "effect disabled", "device disabled",
            "is the plugin on", "turned the plugin off",
        ),
        catalog=(
            "is_active", "switched on", "device on", "parameters[0]",
        ),
        anchors=(
            "device.is_active", "device.on_value", "return_device.is_active",
            "master_device.is_active",
        ),
    ),
    Synonym(
        producer=(
            "what plugins", "whats in the chain", "what effects", "insert list",
            "whats loaded here", "which processors", "whats on this track",
            "what's on this track",
        ),
        catalog=(
            "devices", "device chain", "class_name", "class_display_name",
        ),
        anchors=(
            "track.devices", "device.name", "device.class_name", "device.class_display_name",
            "device.type",
        ),
    ),
    Synonym(
        producer=(
            "automation isnt doing anything", "automation ignored", "stuck at one value",
            "my automation stopped working", "the ride isnt moving",
            "envelope not playing back",
        ),
        catalog=(
            "automation_state", "overridden", "re_enable_automation", "envelope", "playing",
        ),
        anchors=(
            "param.automation_state", "song.re_enable_automation_enabled",
            "song.re_enable_automation", "track.volume_automation_state",
        ),
    ),
    Synonym(
        producer=(
            "knob wont move", "cant change it", "locked", "wont let me set it",
            "greyed out parameter", "something else is controlling it", "macro is driving it",
            "wont budge",
        ),
        catalog=(
            "is_enabled", "param.state", "disabled", "irrelevant", "macro",
        ),
        anchors=(
            "param.is_enabled", "param.state", "track.volume_is_enabled",
        ),
    ),
    Synonym(
        producer=(
            "out of time", "off grid", "rushing", "dragging", "late", "not on the beat",
            "drifting", "timing is off", "not tight", "sloppy",
        ),
        catalog=(
            "clip_trigger_quantization", "launch_quantization", "quantisation", "groove",
            "swing", "warping",
        ),
        anchors=(
            "clip.warping", "song.clip_trigger_quantization", "clip.launch_quantization",
            "clip.has_groove", "song.swing_amount", "song.midi_recording_quantization",
        ),
    ),
    Synonym(
        producer=(
            "stretched", "time stretched", "follows the tempo", "locked to tempo",
            "tempo matched", "elastic audio", "does it change speed",
        ),
        catalog=(
            "warping", "warp_mode", "warp", "tempo",
        ),
        anchors=(
            "clip.warping", "clip.warp_mode", "arrangement_clip.warping",
        ),
    ),
    Synonym(
        producer=(
            "nothing happened when i hit it", "it didnt start", "still blinking",
            "waiting to come in", "hasnt come in yet", "launch didnt take",
        ),
        catalog=(
            "is_triggered", "fired_slot_index", "triggered", "blinking", "quantization",
            "playing_slot_index",
        ),
        anchors=(
            "clip_slot.is_triggered", "track.fired_slot_index", "clip.is_triggered",
            "scene.is_triggered", "track.playing_slot_index",
        ),
    ),
    Synonym(
        producer=(
            "whats running", "whats live right now", "which clip is going", "what is on air",
            "whats currently sounding", "what am i hearing",
        ),
        catalog=(
            "playing_slot_index", "is_playing", "sounding", "currently playing",
        ),
        anchors=(
            "track.playing_slot_index", "clip_slot.is_playing", "clip.is_playing",
            "song.is_playing",
        ),
    ),
    Synonym(
        producer=(
            "cant hear my mic", "no input signal", "input is dead", "cant hear myself",
            "nothing coming in", "mic isnt working", "cant hear the guitar",
        ),
        catalog=(
            "current_monitoring_state", "monitoring", "input_meter_level", "input_routing_type",
            "arriving", "has_audio_input",
        ),
        anchors=(
            "track.current_monitoring_state", "track.input_meter_level",
            "track.input_routing_type", "track.has_audio_input", "track.arm",
        ),
    ),
    Synonym(
        producer=(
            "did it record", "did the take print", "did it capture", "is there a take",
            "did it get recorded", "did anything land",
        ),
        catalog=(
            "is_recording", "session_record_status", "will_record_on_start", "recording",
            "capturing",
        ),
        anchors=(
            "clip_slot.is_recording", "clip.is_recording", "song.session_record_status",
            "clip_slot.will_record_on_start", "clip.will_record_on_start",
        ),
    ),
    Synonym(
        producer=(
            "cpu spike", "crackling", "glitching", "audio dropouts", "stuttering",
            "computer cant handle it", "its overloading", "popping and clicking",
        ),
        catalog=(
            "average_process_usage", "peak_process_usage", "performance_impact",
            "process_usage", "is_frozen",
        ),
        anchors=(
            "app.average_process_usage", "app.peak_process_usage", "track.performance_impact",
            "track.is_frozen",
        ),
    ),
    Synonym(
        producer=(
            "lag", "plugin delay", "sounds late", "delay compensation",
            "behind the beat by a bit", "monitoring delay", "round trip delay",
        ),
        catalog=(
            "latency_in_ms", "latency_in_samples", "latency",
        ),
        anchors=(
            "device.latency_in_ms", "device.latency_in_samples", "return_device.latency_in_ms",
        ),
    ),
    Synonym(
        producer=(
            "live is stuck", "hung", "not responding", "popup", "wont accept anything",
            "spinning", "nothing i do lands", "unresponsive",
        ),
        catalog=(
            "open_dialog_count", "current_dialog_message", "dialog", "modal", "blocked",
        ),
        anchors=(
            "app.open_dialog_count", "app.current_dialog_message",
            "app.current_dialog_button_count",
        ),
    ),
    Synonym(
        producer=(
            "empty slot", "blank", "nothing there", "that square is empty",
            "is anything in that cell", "is there a clip in it",
        ),
        catalog=(
            "has_clip", "is_empty", "holds a clip",
        ),
        anchors=(
            "clip_slot.has_clip", "scene.is_empty", "clip_slot.is_group_slot",
        ),
    ),
    Synonym(
        producer=(
            "count in", "countoff", "pre roll", "preroll", "four bars before it starts",
            "is the click on", "click track",
        ),
        catalog=(
            "metronome", "is_counting_in", "count_in_duration", "count-in",
        ),
        anchors=(
            "song.metronome", "song.is_counting_in", "song.count_in_duration",
        ),
    ),
    Synonym(
        producer=(
            "will it overwrite my take", "does it layer", "stack on top", "punch in",
            "will i lose what is there", "does it replace",
        ),
        catalog=(
            "arrangement_overdub", "overdub", "punch_in", "record_mode", "replaces",
        ),
        anchors=(
            "song.arrangement_overdub", "song.overdub", "song.punch_in", "song.punch_out",
            "clip.is_overdubbing", "song.record_mode",
        ),
    ),
    Synonym(
        producer=(
            "is this midi or audio", "what kind of track", "audio or instrument track",
            "is this a sample or notes", "is it a real instrument",
        ),
        catalog=(
            "has_audio_input", "has_midi_input", "is_midi_clip", "is_audio_clip",
        ),
        anchors=(
            "track.has_audio_input", "track.has_midi_input", "track.has_audio_output",
            "clip.is_midi_clip", "clip.is_audio_clip",
        ),
    ),
    Synonym(
        producer=(
            "flattened", "committed", "bounced in place", "printed",
            "why cant i edit the devices", "its baked", "rendered",
        ),
        catalog=(
            "is_frozen", "frozen", "can_be_frozen",
        ),
        anchors=(
            "track.is_frozen", "track.can_be_frozen",
        ),
    ),
    Synonym(
        producer=(
            "cut short", "stops early", "runs out", "doesnt loop round", "only plays once",
            "truncated", "ends too soon",
        ),
        catalog=(
            "looping", "loop_end", "end_marker", "length",
        ),
        anchors=(
            "clip.looping", "clip.loop_end", "clip.end_marker", "clip.length",
        ),
    ),
    Synonym(
        producer=(
            "does anything move in this clip", "any rides", "is there automation drawn",
            "any curves in here", "did i automate anything here",
        ),
        catalog=(
            "has_envelopes", "envelopes", "automation", "automation_envelopes",
        ),
        anchors=(
            "clip.has_envelopes", "clip.automation_envelopes",
        ),
    ),
    Synonym(
        producer=(
            "dynamics are flat", "velocity isnt doing anything", "no dynamics",
            "hits are all the same", "my velocities did nothing",
        ),
        catalog=(
            "velocity_amount", "velocity", "inaudible",
        ),
        anchors=(
            "clip.velocity_amount",
        ),
    ),
    Synonym(
        producer=(
            "what am i looking at", "which track is selected", "the one im on",
            "whats in focus", "the clip im editing", "this one here",
        ),
        catalog=(
            "selected_track", "detail_clip", "highlighted_clip_slot", "selection",
        ),
        anchors=(
            "view.selected_track", "view.detail_clip", "view.highlighted_clip_slot",
            "view.selected_track_devices",
        ),
    ),
)


def _expand_table(table: tuple[Synonym, ...]) -> dict[str, list[str]]:
    """Flatten the synonym table into the lookup the scorer uses."""
    out: dict[str, list[str]] = {}
    for entry in table:
        for word in entry.producer:
            out.setdefault(word, [])
            for term in entry.catalog:
                if term not in out[word]:
                    out[word].append(term)
    return out


#: Producer term to catalog terms, flattened once at import.
SYNONYMS: dict[str, list[str]] = _expand_table(_SYNONYM_TABLE)


def all_anchor_ids() -> list[str]:
    """Return every catalog row id these tables claim to reach, for the test that checks them."""
    return sorted({anchor for entry in _SYNONYM_TABLE for anchor in entry.anchors})
