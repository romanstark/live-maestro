---
name: Bug report
about: Something doesn't work as expected
labels: bug
---

**Before anything else: the three questions that solve most reports**

Remote Scripts load only when Live starts, and Live silently prefers a compiled `__pycache__` over your source. Almost every "it stopped working" is one of these:

- [ ] The Remote Script was **copied to the User Library** after the last change (`<User Library>\Remote Scripts\LiveMaestro\__init__.py`). `Preferences\Library.cfg` records `<ProjectPath>` and `<ProjectName>`, and the User Library is one or the other of `<ProjectPath>` and `<ProjectPath>\<ProjectName>`, so run `python scripts/install_script.py --list` rather than reading it off by hand
- [ ] `__pycache__` next to it was **deleted**
- [ ] Live was **restarted completely** afterwards
- [ ] `LiveMaestro` is selected under Preferences → Link, Tempo & MIDI → *Control Surface*

If you are not sure, do all four and try again, then report either way, because "the instructions were followed and it still failed" is useful information.

**What happened** A clear description of the bug.

**To reproduce** The tool call or command you ran (e.g. `lom_set("song.tracks[0].mixer_device.volume.value", 0.9)`), and the state of the set it ran against.

**Expected** What you expected to happen.

**What came back** The full response, including `status`, `code` and `message`, and for a write the `before` / `after` / `clamped` fields. Please paste it rather than summarising it. The difference between "accepted", "read back" and "no change" is exactly what is being diagnosed.

**Environment**
- Ableton Live version (e.g. Live 12.4.3):
- OS (and version):
- Python version:
- live-maestro version / commit:
- Remote Script reinstalled + Live restarted after the last change: yes / no
- Is another Remote Script also installed and running (port 9877 is a common one)?
- Output of `python scripts/inventory.py --installation-only`, which reports the installed Live versions, the User Library it resolved, whether the script is there and current, and whether the port is listening.

**Log** Anything relevant from Live's `Log.txt`, and from the server's own output.
