# Upgrading to a new Ableton Live version

A runbook for re-verifying this project against a new Live release. It covers deploying the Remote Script, reconciling the catalog against the new object model, re-measuring every row, and updating the numbers the test suite asserts.

Every command below was checked against the scripts in this repository. Flags that look plausible and do not exist are listed in [Appendix A](#appendix-a-flags-that-do-not-exist), because three of them are easy to reach for.

All invocations go through the repository's virtual environment, written here in the Windows form `.venv\Scripts\python.exe`. Substitute your own paths where a value appears in angle brackets.

---

## 0. What the upgrade has to produce

| Artefact | State after a complete run |
|---|---|
| `live-remote-script/__init__.py` | Installed under the new Live version's User Library, byte-identical to the repository |
| `src/live_maestro/catalog/*.yaml` | Rows added, removed or re-statused to match the new object model, each `proven` field written from a run |
| `measurements/` | Exactly one report, `catalog-live-<new version>.json` |
| `docs/limits.md` | Limits that the new version lifted are marked as lifted, with the measurement that showed it |
| Prose that states a current version | Updated. Prose that records a past measurement is left alone. See [step 8](#8-update-the-prose-that-states-a-version) |
| `pytest`, `ruff`, `pyright` | Green |

---

## 1. Prerequisites

- The new Live version installed and started at least once, so that it has written its own `Library.cfg` (under `Preferences/` on Windows, directly in the version folder on macOS). Discovery reads that file, and a version that has never fully started has none.
- A test project you are willing to have written to. Every property write is read back and restored, and a crash mid-probe is not. Use a copy.
- The repository's virtual environment, with `pytest`, `ruff` and `pyright` available.

Confirm the toolchain before touching Live:

```powershell
.venv\Scripts\pytest
.venv\Scripts\ruff check src scripts tests
.venv\Scripts\pyright
```

A red suite before the upgrade makes every later failure ambiguous.

---

## 2. Prepare the specimen set

`scripts/probe_paths.py` aims each row at an object it picks out of the open set. Where the object is absent the row reports `inconclusive`, which is not a failure and not a measurement either. The set therefore decides how much of the catalog a run can reach.

The table lists what has to be present, how many rows depend on it, and who can build it. The counts come from the catalog's own path placeholders.

| Object | Rows depending on it | Built by |
|---|---:|---|
| A non-group MIDI track, first among the non-group tracks | 636 | script |
| An audio track | part of the 636 | script |
| No group track at a low index, because it changes what every auto-pick lands on | all `{track}` rows | keep the set free of one |
| At least one return track | 152, plus 32 send rows | script |
| A device on the first return track | 31 | script, load onto the selected track |
| A device on the Main track | 34 | script, same |
| Eight scenes, which a fresh set already has | 14, and it gates the clip scan | script |
| A Session audio clip, warped, with at least two warp markers | tried first for 90 clip rows, and the 4 warp-marker rows | script for the clip, Live decides the warping |
| A Session MIDI clip with notes at more than one pitch | the 6 MIDI note calls and every MIDI-only clip row | script |
| A Session clip carrying an automation envelope | `clip.clear_envelope` and its arrangement twin | script |
| An Arrangement clip at index 0 of the first non-group track | 85 | script |
| A plain device at a known index, such as an EQ Eight | 30 device rows, 16 parameter rows, the routing rows | script |
| An Instrument or Audio Effect Rack with at least one chain holding a device | 93 chain rows, 24 rack rows, 48 nested rows | preset or person |
| A rack with at least one internal return chain | 8 | **person** |
| A bare Drum Rack with pad 36 filled | 25 | preset or person |
| A kit preset, meaning a Drum Rack nested inside an Instrument Rack, with pad 36 filled | 6, plus 5 nested-Simpler rows | preset or person |
| A Simpler with a sample loaded | 45, plus 4 sample rows | script loads the device, the sample is a drag |
| A Sampler, a Wavetable, a Hybrid Reverb, a Looper | 1, 15, 8 and 5 | script |
| At least one locator in the arrangement | 3 | script |
| At least one groove in the Groove Pool | 11 | **person** |

### What a script can and cannot build

A script can create tracks, return tracks and scenes, create clips, write notes and envelopes, set names, park the playhead, and load a device from the browser onto the selected track. `.adg` files in the browser are device groups and arrive with their chains and pads already populated, which is how a rack with chains is built without touching the interface.

Four things the LOM cannot do, so a person does them in Live:

1. **A return chain inside a rack.** Right-click in the rack's chain list, "Create Return Chain". No LOM method creates one.
2. **A bare Simpler on a drum pad.** Drag any sample onto an empty pad. Every factory kit wraps its pads in an Instrument Rack instead, so a preset does not produce this shape.
3. **A group track**, if `track.fold_state` is to be measured. Select two tracks and press Ctrl+G. Grouping is read-only in the LOM (see [limits.md section 2](limits.md)).
4. **A groove in the Groove Pool.** Drag a groove file into the pool.

Loading a device *inside* a rack chain is also out of reach from here: writing `rack.view.selected_chain` reads back correctly and the load still lands on the track. [limits.md section 2](limits.md) records the measurement.

### Recommendation

Build the specimen set once, save it under a name of its own, and reuse it for every upgrade. Three of the four manual steps survive a save, so a saved specimen set turns them into a one-time cost. Note in the commit which specimen set a report was measured against.

---

## 3. Deploy the Remote Script

Discovery reads every `Live <version>` folder under the preferences root and ranks the User Library candidates it finds. Look before installing:

```powershell
.venv\Scripts\python.exe scripts/install_script.py --list
.venv\Scripts\python.exe scripts/install_script.py --list --limit 50
```

`--list` prints each candidate with the evidence for it and marks the best. Where several candidates exist the installer asks, because Live reads exactly one of them and from the wrong one it loads nothing and says nothing.

Preview, then install:

```powershell
.venv\Scripts\python.exe scripts/install_script.py --dry-run
.venv\Scripts\python.exe scripts/install_script.py
```

Non-interactive callers pass either `--yes`, which takes the best-evidence candidate, or `--user-library "<your User Library>"`, which names one outright. Without a terminal and without one of those flags the installer refuses rather than guessing.

Then, in Live:

1. Quit Live completely and start it again. Remote Scripts load only at startup. There is no reload and no rescan.
2. Open Preferences, Link Tempo and MIDI, and select **LiveMaestro** in a Control Surface slot. The folder name under `Remote Scripts/` is what appears in that dropdown.

Confirm the installed file matches the repository:

```powershell
.venv\Scripts\python.exe scripts/install_script.py --diff
```

Exit code 0 means identical, 1 means they differ. A prose-only difference is harmless and a logic difference is not, so read what it prints rather than the exit code alone.

---

## 4. Verify the connection

```powershell
.venv\Scripts\python.exe -m live_maestro.client ping
.venv\Scripts\python.exe -m live_maestro.client info
```

`info` reports the Live version, the script version, the protocol version and the allowlist size. Record the Live version it prints: it names the measurement report in step 6 and it is what the report carries.

For a fuller picture of the installation and the open set:

```powershell
.venv\Scripts\python.exe scripts/inventory.py --installation-only
.venv\Scripts\python.exe scripts/inventory.py
.venv\Scripts\python.exe scripts/inventory.py --json
```

`--installation-only` skips the session scan. It still probes the port, so Live has to be running.

---

## 5. Reconcile the catalog against the new object model

`scripts/sync_catalog.py` walks the running object model with `lom_describe` and compares it against the catalog. It finds properties the catalog lacks and rows the object model no longer has.

**This script has no `--dry-run` and no `--go`. A bare invocation connects to Live immediately.** `--report` is the read-only mode:

```powershell
.venv\Scripts\python.exe scripts/sync_catalog.py --report
.venv\Scripts\python.exe scripts/sync_catalog.py --report --verbose
```

Keep the raw answers, so the reconciliation can be repeated offline without asking Live again:

```powershell
.venv\Scripts\python.exe scripts/sync_catalog.py --record <run>.json --report
.venv\Scripts\python.exe scripts/sync_catalog.py --replay <run>.json --verbose
```

Capture the deletion block for `docs/limits.md` **before** removing anything, because a path that has gone is documented there rather than kept as a row:

```powershell
.venv\Scripts\python.exe scripts/sync_catalog.py --limits-report <block>.md --report
```

Then apply one class of change at a time:

```powershell
.venv\Scripts\python.exe scripts/sync_catalog.py --add
.venv\Scripts\python.exe scripts/sync_catalog.py --delete
.venv\Scripts\python.exe scripts/sync_catalog.py --status
```

`--write` is the union of `--add` and `--delete`. Applying them separately keeps the diff readable, which matters when a release moves many rows.

### Two things to know before running it

**New rows arrive as `verified` with no `proven`.** The script writes `status: verified` for a property it read through `lom_describe`, and it does not write the `proven` field. `tests/test_catalog.py` requires every `verified` row to be backed by a run, so **the suite is red between this step and step 6**. That is the intended signal: a row that has been described has not been exercised.

**A removed row can take a lookup anchor with it.** `src/live_maestro/vocabulary.py` points producer words at catalog row ids, and `tests/test_lookup.py` fails when an anchor names a row that is gone or one whose status is no longer `verified`. After `--delete`, run that test and remove the orphaned anchors:

```powershell
.venv\Scripts\pytest tests/test_lookup.py -q
```

---

## 6. Measure every row

The whole catalog is `--all`, not an area named `all`:

```powershell
.venv\Scripts\python.exe scripts/probe_paths.py --all --dry-run
```

The dry run opens no socket and prints the plan per row. Read it before the first real run.

A measurement run needs `--go`. Start read-only, which writes nothing to the set:

```powershell
.venv\Scripts\python.exe scripts/probe_paths.py --all --go --read-only --i-know-what-im-doing --json > runs\read.json
```

`--i-know-what-im-doing` is required whenever the open set looks like real work. Then the write pass, area by area, so that one abort does not cost the whole run:

```powershell
.venv\Scripts\python.exe scripts/probe_paths.py --area song --go --i-know-what-im-doing --json > runs\song.json
.venv\Scripts\python.exe scripts/probe_paths.py --area track --track 1 --go --i-know-what-im-doing --json > runs\track.json
```

Every path placeholder has a flag of its own, generated from the catalog: `--track`, `--slot`, `--clip`, `--device`, `--chain`, `--nested`, `--pad`, `--param`, `--ret`, `--return`, `--return-chain`, `--send`, `--scene`, `--cue`, `--marker`, `--macro`, `--groove`, `--index`, `--child`, `--grandchild`, `--root`. Use them to aim a row at a suitable object rather than the first one the set offers. Aiming badly is the single largest source of false `broken` verdicts.

Three further classes need explicit consent:

| Flag | What it unlocks |
|---|---|
| `--include-calls` | Actually invokes `call` rows. Without it they are described and not called, because a call has no read-back and no restore |
| `--include-transport` | Writes that start playback, recording or arming |
| `--include-destructive` | Rows marked `destructive`. Each one additionally needs its row id typed at an interactive terminal, so this does not work from a non-interactive shell |

Pacing defaults to `--pace 0.15` and `--device-pace 0.75`. Leave them unless a run is stalling.

Do **not** pass `--write-back` on a first pass. It appends to `doc` and rewrites `status` and `proven` per row, and a run aimed at the wrong objects would write wrong statuses. Measure first, read the reports, then write back once the aiming is right.

### Respect what earlier runs measured

`scripts/probe_paths.py` holds several sets of row ids near the top of the file, each recording a hazard found by running into it. A new Live version does not invalidate them, so re-measure before removing an entry:

- `MODAL_RISK_ROWS`: writing these can put a modal dialog on screen, which owns Live's main thread and stops the socket answering until a person clicks it away.
- `ONE_WAY_ROWS`: Live owns the value and the restoring write is accepted and ignored.
- `READ_IS_NOT_A_WRITE`: the value a read returns is not a value a write accepts.
- `WRITE_IS_NOT_A_ROUND_TRIP`: a write is accepted and stores something else.
- `ACTION_ROWS`: writing these starts playback, recording or arming.
- `GUARD_LIMITS`: the thresholds that make the script refuse a set that looks like real work.

---

## 7. Write the measurement report

`probe_paths.py --json` prints one run. The report the tests read is the fold of every run:

```powershell
.venv\Scripts\python.exe scripts/write_measurements.py runs\*.json
```

The tool names the file from the `live_version` the runs carry, so it writes `measurements/catalog-live-<new version>.json` without being told the version. It prints the outcome counts and refuses to write if any value read out of the set has leaked into the report.

**Delete the superseded report in the same commit.** `tests/test_catalog.py` resolves the report by looking for exactly one `catalog-live-*.json` and fails on any other count. One report describes the version the catalog currently stands on.

Then write the fields back into the catalog:

```powershell
.venv\Scripts\python.exe scripts/probe_paths.py --all --go --i-know-what-im-doing --write-back
```

`--write-back` requires `--go`. It rewrites `status`, rewrites or inserts `proven`, and appends a sentence to `doc` only where the run learned something the fields cannot hold, such as a clamp.

---

## 8. Review the limits

Read [docs/limits.md](limits.md) top to bottom against the new version. Sections 1 and 2 are the ones a release can change: a limit Live lifts is a row this project can gain.

For each entry, either leave it standing or retract it the way it was made, by asking Live again and writing down the answer. Leave the entry in place when it is lifted rather than deleting it: somebody planned around that limit and needs to find out it is gone. `docs/limits.md` already carries device reordering that way, with the call that disproved it stated in place.

Section 5 lists open questions with the procedure that would settle each. A new version is the occasion to run those procedures.

---

## 9. Update the prose that states a version

This is the step most likely to go wrong, because two kinds of sentence mention a Live version and only one of them may change.

**A sentence that records when something was measured keeps its version.** "Measured 2026-09-05 against Live 12.4.5: a 4-beat clip became 8 beats" stays true under Live 13. It is a record of a run, not a claim about the present. The overwhelming majority of version mentions in this repository are of this kind.

**A sentence that states what is true now moves.** Find them with:

```powershell
.venv\Scripts\python.exe -c "import re,pathlib; [print(f'{p}:{i}: {l.strip()[:100]}') for p in pathlib.Path('.').rglob('*') if p.suffix in ('.md','.py','.yaml') and '.venv' not in p.parts for i,l in enumerate(p.read_text(encoding='utf-8').splitlines(),1) if re.search(r'Live\s+\d+\.\d+', l) and not re.search(r'(Measured|verified|probed)\s+\d{4}-\d\d-\d\d', l)]"
```

Read each hit and decide. The ones that state a current version are few, and the catalog file headers and `docs/catalog.md` are where they concentrate.

**A version inside test data never moves.** `tests/test_als.py` carries `Creator="Ableton Live <version>"` inside fixture XML. Those strings are inputs, and changing them changes what the test exercises.

### Numbers the tests hold you to

`tests/test_repository_claims.py` compares figures in prose against the code and fails when they disagree. It recognises several sentence shapes for the catalog total, the verified count, the allowlist size and the tool count. After the catalog changes, run it and fix what it names:

```powershell
.venv\Scripts\pytest tests/test_repository_claims.py -q
```

It does not catch every phrasing. When you write a new figure into a document, prefer a shape it already knows, so the next release cannot leave it stale in silence.

`tests/test_catalog.py` holds `UNMEASURED_VERIFIED_ALLOWANCE`, the number of `verified` rows with nothing checkable behind them. It is zero. Raising it is a statement that a row claims a run nobody made, so the fix is a measurement rather than a larger allowance.

---

## 10. Run the suite and commit

```powershell
.venv\Scripts\pytest
.venv\Scripts\ruff check src scripts tests
.venv\Scripts\pyright
```

`pytest` piped into another command hides its exit code, so read the summary line or check the code directly.

One commit per class of change keeps the diff reviewable: the Remote Script deployment, the catalog reconciliation, the measurement report, the limits review, the prose. Say in the commit message which Live version and which operating system the run was made on, and which specimen set it used.

---

## Ordering, and why it is fixed

1. **Deploy and restart before anything reads Live.** The old script answers on the same port, so a sync or a probe against a stale script measures the old script's behaviour under the new Live.
2. **Reconcile before measuring.** `probe_paths.py` measures the rows that exist. A row `sync_catalog.py` has not yet added cannot be measured, and one it will delete is measured for nothing.
3. **Measure before writing back.** `--write-back` writes a status per row. A run aimed at the wrong objects writes wrong statuses, and a wrong `verified` is worse than a missing row.
4. **Write the report before running the suite.** The report is what `proven` is checked against.

The suite is red from step 5 until step 7. That window is expected.

---

## Appendix A: flags that do not exist

Each of these is a plausible guess that fails. The right-hand column is the working form.

| Does not exist | Use instead |
|---|---|
| `probe_paths.py --area all` | `probe_paths.py --all`. `--area` is validated against real area names and `all` is not one |
| `probe_paths.py --write` | `probe_paths.py --write-back`, which also requires `--go` |
| `probe_paths.py --report` | `--json` on stdout. `--report` belongs to `sync_catalog.py` |
| `probe_paths.py --record` / `--replay` / `--specimens` / `--limits-report` | All four are `sync_catalog.py` flags |
| `probe_paths.py --verbose` | Does not exist. Only `sync_catalog.py` has it |
| `sync_catalog.py --dry-run` / `--go` | Neither exists. `--report` is the read-only mode, and a bare invocation connects |
| `sync_catalog.py --all` | Not needed. A bare invocation already covers the whole catalog |
| `sync_catalog.py --write-back` | `--write`, or `--add` and `--delete` separately |
| `install_script.py --install` | Installing is the default with no mode flag |
| `install_script.py --force` / `--overwrite` / `--no-backup` | None exist. A differing file is always backed up first |
| `install_script.py --live-version` | None exists. Discovery reads every Live version folder it finds |
| `inventory.py --dry-run` / `--offline` / `--no-socket` | None exist. The script is read-only by construction and always probes the port |

## Appendix B: quick reference

```powershell
# toolchain
.venv\Scripts\pytest
.venv\Scripts\ruff check src scripts tests
.venv\Scripts\pyright

# deployment
.venv\Scripts\python.exe scripts/install_script.py --list
.venv\Scripts\python.exe scripts/install_script.py --dry-run
.venv\Scripts\python.exe scripts/install_script.py
.venv\Scripts\python.exe scripts/install_script.py --diff

# connection
.venv\Scripts\python.exe -m live_maestro.client ping
.venv\Scripts\python.exe -m live_maestro.client info
.venv\Scripts\python.exe scripts/inventory.py --installation-only

# catalog reconciliation
.venv\Scripts\python.exe scripts/sync_catalog.py --report --verbose
.venv\Scripts\python.exe scripts/sync_catalog.py --limits-report <block>.md --report
.venv\Scripts\python.exe scripts/sync_catalog.py --add
.venv\Scripts\python.exe scripts/sync_catalog.py --delete

# measurement
.venv\Scripts\python.exe scripts/probe_paths.py --all --dry-run
.venv\Scripts\python.exe scripts/probe_paths.py --all --go --read-only --i-know-what-im-doing --json > runs\read.json
.venv\Scripts\python.exe scripts/probe_paths.py --area <area> --go --i-know-what-im-doing --json > runs\<area>.json
.venv\Scripts\python.exe scripts/write_measurements.py runs\*.json
.venv\Scripts\python.exe scripts/probe_paths.py --all --go --i-know-what-im-doing --write-back

# the count that will need a hand
.venv\Scripts\python.exe -c "from live_maestro.registry import default_registry as d; r=d(); print(len(r.all()), r.status_counts())"
```
