"""The repository's claims about itself, checked against the repository.

Prose goes stale in a way code does not: a number is right on the day it is written
and wrong from the next commit onward, a citation survives the section it names
being renamed, and a pasted path carries somebody's account name. A test that only
exercises behaviour sees none of it. Each test here closes one class:

* a count in prose that the code contradicts, such as an allowlist size or a row
  total,
* a citation that does not resolve: a document that is not tracked, a filename that
  does not exist, or a section-numbering form the cited document does not use,
* a local path or account name pasted into a shipped file.

They are deliberately narrow: they match the sentence shapes this repository writes.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from live_maestro.registry import default_registry

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "live-remote-script" / "__init__.py"
SERVER = ROOT / "src" / "live_maestro" / "server.py"


def tracked_files(*suffixes: str) -> list[Path]:
    """Files git actually tracks. Anything else is not published.

    These guards police what the repository publishes, so they ask git rather than the
    filesystem. An unpacked sdist is not a repository, which a packager running the
    suite will hit: there the question cannot be asked at all, so the test skips rather
    than crashing on ``git ls-files``.
    """
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("not a git checkout, so there is nothing to ask git about")
    return [ROOT / f for f in probe.stdout.split() if f.endswith(suffixes)]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def fail(headline: str, offences: list[str]) -> None:
    if offences:
        body = "\n".join(f"  - {line}" for line in offences)
        pytest.fail(f"{headline}:\n{body}", pytrace=False)


# --------------------------------------------------------------------------------
# Counts written into prose
# --------------------------------------------------------------------------------


def _literal_elements(node: ast.expr) -> list[ast.expr]:
    """Return the element nodes of a literal collection, unwrapping one call.

    The Remote Script writes one of these two as a frozenset around a literal
    and the other as a bare literal, so the shape to read through is either a
    call wrapping a sequence or the sequence itself. Anything else is a change
    to the script that this check has to notice rather than walk past.
    """
    if isinstance(node, ast.Call) and node.args:
        node = node.args[0]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return list(node.elts)
    raise AssertionError(
        f"expected a literal collection, found {type(node).__name__}"
    )


def allowlist_entries() -> set[str]:
    """The frozenset the Remote Script actually checks, read off its AST.

    Read rather than imported: the script runs inside Live's interpreter and
    imports ``Live`` and ``_Framework``, neither of which exists out here.
    """
    for node in ast.walk(ast.parse(read(SCRIPT))):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "METHOD_ALLOWLIST" for t in node.targets
        ):
            return {ast.literal_eval(e) for e in _literal_elements(node.value)}
    raise AssertionError("METHOD_ALLOWLIST not found in the Remote Script")


def test_counts_in_prose_match_what_the_code_holds() -> None:
    """Every number the repository states about itself, recomputed.

    The patterns below are sentence shapes, not expected numbers: each finds where a
    count is being asserted and the test then compares whatever number is there
    against the code. A correct figure in a sentence nobody predicted therefore does
    not have to be listed here. The set is narrow by design.
    """
    registry = default_registry()
    rows = len(registry.all())
    verified = registry.status_counts()["verified"]
    allow = allowlist_entries()
    tools = len(re.findall(r"^@mcp\.tool", read(SERVER), re.MULTILINE))

    # Each shape has to name the whole catalog, not a count of some subset. "the
    # catalog carried 25 rows for properties that do not exist" is a measurement of
    # one reconciliation run and "36 rows -- every mixer fader" is a family. Both
    # are true and neither is the total, so neither may be matched here.
    shapes: list[tuple[re.Pattern[str], int, str]] = [
        (re.compile(r"(\d[\d,]*)\s+rows across"), rows, "catalog rows"),
        (re.compile(r"(\d[\d,]*)\s+rows\s*[-–—]\s*\d[\d,]*\s+`?verified"), rows, "catalog rows"),
        (re.compile(r"(\d[\d,]*)\s+rows,\s*\d[\d,]*\s+are"), rows, "catalog rows"),
        # The same sentence shape, capturing the second number. The shape above
        # asserts only its own group(1), so a sentence like "1163 rows, 1126 are
        # verified" has its total checked and its verified count ignored. Anchored
        # on "rows," so it fires only
        # where the sentence already named the whole catalog: a family count such as
        # "31 of the 40 device rows are verified" must not be compared to the total.
        (re.compile(r"\d[\d,]*\s+rows,\s*(\d[\d,]*)\s+are\s+`{0,2}verified"),
         verified, "verified catalog rows"),
        # "1,166 rows across 5 files, with 1,130 verified" is the same claim in the
        # other word order, and the shape above does not reach it. Still anchored on
        # "rows" so that a family count keeps its own number, and capped at a short
        # gap so that two unrelated sentences cannot be joined into a match.
        (re.compile(r"\d[\d,]*\s+rows\b[^.]{0,40}?with\s+(\d[\d,]*)\s+`{0,2}verified"),
         verified, "verified catalog rows"),
        (re.compile(r"(\d[\d,]*)\s+addressable places"), rows, "catalog rows"),
        (re.compile(r"it is (\d[\d,]*) rows\b"), rows, "catalog rows"),
        # "all N rows" and "in N rows" both name the whole catalog, and both went
        # stale while every shape above passed. "all" and "in" are
        # required so that a family count keeps its own number: "in 36 rows of
        # the device area" does not start with either word directly before the
        # figure.
        (re.compile(r"\ball (\d[\d,]*)\s+rows\b", re.IGNORECASE), rows, "catalog rows"),
        (re.compile(r"\bin (\d[\d,]*)\s+rows\b", re.IGNORECASE), rows, "catalog rows"),
        (re.compile(r"(\d[\d,]*)\s+entries covering"), len(allow), "allowlist entries"),
        # The handshake example in protocol.md quotes the real size. Section 5.1 marks
        # script_version in the same examples as a placeholder and says nothing about
        # this one, so it reads as fact and has to be one.
        (re.compile(r'"allowlist_size":\s*(\d[\d,]*)'), len(allow), "allowlist entries"),
        (re.compile(r"\*\*(\d[\d,]*) tools\b"), tools, "MCP tools"),
    ]

    offences = []
    for path in tracked_files(".md", ".py", ".toml"):
        if path.name == Path(__file__).name:
            continue
        text = read(path)
        for pattern, expected, what in shapes:
            for m in pattern.finditer(text):
                got = int(m.group(1).replace(",", ""))
                if got != expected:
                    line = text[: m.start()].count("\n") + 1
                    offences.append(
                        f"{path.relative_to(ROOT).as_posix()}:{line}: says {got} {what}, "
                        f"the code holds {expected}"
                    )
    fail("count(s) in prose that the code contradicts", offences)


# --------------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------------

_DOC_REF = re.compile(r"docs/(\w[\w-]*\.md)")
_SECTION_REF = re.compile(r"docs/(\w[\w-]*\.md)[\s`\"']*(?:§|section\s+)(\d+)", re.IGNORECASE)
_MD_LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)#]+)")

#: A quoted section name cited beside a document, with or without a section number in
#: between: ``docs/architecture.md, 'the restart tax'`` and
#: ``docs/limits.md §5, "Cost of introspection and of listeners"`` both match. The gap
#: is kept short so an unrelated quotation later in the same sentence is not read as
#: part of the citation.
_SECTION_NAME_REF = re.compile(
    r"docs/(\w[\w-]*\.md)[^\n'\"]{0,24}?['\"]([^'\"\n]{4,60})['\"]"
)

#: A quoted string that is a template for the reader to fill in, not a citation:
#: ``'<Class>.<method>'`` tells a user what to type. Angle brackets are the marker.
_PLACEHOLDER = re.compile(r"[<>]")


#: Working documents that live in the tree and are deliberately kept out of the
#: repository: planning notes, private specifications, and the assistant's own brief.
#: Using an assistant is not part of what this server does, and a half-finished
#: specification is not something a reader should be sent to. They are named here
#: rather than read from .git/info/exclude, because that file is local to one
#: checkout and this guard has to mean the same thing everywhere.
DELIBERATELY_UNTRACKED = (
    "CLAUDE.md",
    "Tasks.md",
    "docs/PRD.md",
    "docs/audio_perception_spec.md",
    "docs/implementation_plan.md",
    "docs/live-maestro-contract-hardening-spec.md",
    "docs/live-maestro-instrument-sweep-spec.md",
    "docs/live-maestro-sound-rebuild-spec.md",
)

#: Files that name a deliberately untracked document in order to keep it out, and are
#: therefore not citing it. The build configuration has to spell the names, because the
#: sdist include list names directories and hatchling fills them from the filesystem.
NAMES_THEM_TO_KEEP_THEM_OUT = ("pyproject.toml",)

#: Suffixes a citation can hide in. JSON is here because the measurement records are
#: JSON and cite the prose they went into, which is a contract their own README makes.
CITING_SUFFIXES = (".md", ".py", ".yaml", ".toml", ".yml", ".json")


def test_the_sdist_does_not_ship_a_working_document() -> None:
    """A document kept out of the repository must not reach the package either.

    The sdist include list names directories, and hatchling fills them from the
    filesystem rather than from git, so a file that is untracked is still packaged.
    The build has to exclude them by name, and this fails when a name is added to
    :data:`DELIBERATELY_UNTRACKED` and not to that list.
    """
    config = tomllib.loads(read(ROOT / "pyproject.toml"))
    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    included = {entry.split("/")[0] for entry in sdist["include"]}
    excluded = set(sdist.get("exclude", []))
    offences = [
        f"{name} is deliberately untracked, sits under the packaged {name.split('/')[0]}/ "
        "directory, and is not in the sdist exclude list"
        for name in DELIBERATELY_UNTRACKED
        if "/" in name and name.split("/")[0] in included and name not in excluded
    ]
    fail("working document(s) the sdist would ship", offences)


def test_every_cited_document_is_tracked() -> None:
    """Verify cited documentation files are tracked in version control.

    Existing on this disk is not the test. The documents in
    :data:`DELIBERATELY_UNTRACKED` sit in the working tree and would never reach a
    clone. A tracked file citing one of them is a broken link for everybody but the
    author, and checking the filesystem cannot see it.
    """
    tracked = {
        p.relative_to(ROOT).as_posix() for p in tracked_files(*CITING_SUFFIXES)
    }
    offences = []
    for path in tracked_files(*CITING_SUFFIXES):
        if path.name in (Path(__file__).name, *NAMES_THEM_TO_KEEP_THEM_OUT):
            continue
        text = read(path)
        for m in _DOC_REF.finditer(text):
            cited = f"docs/{m.group(1)}"
            if cited not in tracked:
                why = "is not tracked" if (ROOT / cited).exists() else "does not exist"
                offences.append(f"{path.relative_to(ROOT).as_posix()}: cites {cited}, which {why}")
        for name in DELIBERATELY_UNTRACKED:
            if name in text:
                offences.append(
                    f"{path.relative_to(ROOT).as_posix()}: mentions {name}, which is "
                    "deliberately untracked and will not exist for a reader who clones"
                )
    fail("citation(s) a clone would not resolve", sorted(set(offences)))


def test_every_cited_section_number_exists() -> None:
    """``docs/protocol.md §8`` has to be a heading in that file.

    A section number survives a document being reorganised, and a reader sent to
    the wrong section reads the wrong rule with no sign anything is amiss.
    """
    headings: dict[str, set[str]] = {}
    for doc in (ROOT / "docs").glob("*.md"):
        headings[doc.name] = set(re.findall(r"^##\s+(\d+)\.", read(doc), re.MULTILINE))

    offences = []
    for path in tracked_files(".md", ".py", ".yaml", ".toml", ".yml"):
        for m in _SECTION_REF.finditer(read(path)):
            doc, number = m.group(1), m.group(2)
            if doc in headings and headings[doc] and number not in headings[doc]:
                offences.append(
                    f"{path.relative_to(ROOT).as_posix()}: cites docs/{doc} section {number}, "
                    f"which has sections {sorted(headings[doc], key=int)}"
                )
    fail("citation(s) to a section that is not there", sorted(set(offences)))


def test_every_cited_section_name_exists() -> None:
    """``docs/limits.md §5, 'Cost of introspection'`` has to be findable in that file.

    The sibling above checks the number. This checks the name beside it, which is the
    half that rots when a document is reorganised rather than renumbered. A section
    cited by a name the document no longer carries reads as a precise reference and
    resolves to nothing.

    A phrase counts as present if it appears in a heading or anywhere in the prose, so
    quoting a sentence rather than a heading is allowed. Only a phrase that is nowhere
    in the cited document fails.
    """
    docs = {p.name: read(p) for p in (ROOT / "docs").glob("*.md")}
    docs["README.md"] = read(ROOT / "README.md")

    offences = []
    for path in tracked_files(".md", ".py", ".yaml", ".toml", ".yml"):
        if path.name == Path(__file__).name:
            continue
        for m in _SECTION_NAME_REF.finditer(read(path)):
            doc, phrase = m.group(1), m.group(2).strip()
            if doc not in docs or not phrase or _PLACEHOLDER.search(phrase):
                continue
            if phrase.casefold() not in docs[doc].casefold():
                offences.append(
                    f"{path.relative_to(ROOT).as_posix()}: cites docs/{doc} "
                    f"{phrase!r}, which is not in that document"
                )
    fail("citation(s) naming a section that is not there", sorted(set(offences)))


def test_every_relative_markdown_link_resolves() -> None:
    """A link is relative to the file that holds it, which is where these break."""
    offences = []
    for path in tracked_files(".md"):
        for m in _MD_LINK.finditer(read(path)):
            target = m.group(1).strip()
            if not target or " " in target:
                continue  # not a path: parenthesised prose
            if not (path.parent / target).exists():
                offences.append(
                    f"{path.relative_to(ROOT).as_posix()}: link to {target} does not resolve"
                )
    fail("markdown link(s) that do not resolve", sorted(set(offences)))


# --------------------------------------------------------------------------------
# What must not be published
# --------------------------------------------------------------------------------

#: A Windows or POSIX home directory with a real account name in it. Probe output
#: pasted into a doc is how these arrive: one catalog row carried
#: 'C:\Users\<name>\Music\Live\...' as the measured value of song.file_path.
_HOME_PATH = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/(?:home|Users)/)([A-Za-z0-9._-]+)")

#: Environment variables and documentation placeholders are the correct way to
#: write these, and must not trip the test.
_PLACEHOLDER = re.compile(r"%\w+%|\$\{?\w+\}?|<[^>]+>|\byour\b|\busername\b", re.IGNORECASE)


def test_no_shipped_file_carries_a_home_directory_path() -> None:
    """An account name in a published file is somebody's, and it is not the reader's."""
    offences = []
    for path in tracked_files(".md", ".py", ".yaml", ".yml", ".toml", ".cfg", ".txt"):
        if path.name == Path(__file__).name:
            continue
        for i, line in enumerate(read(path).splitlines(), 1):
            m = _HOME_PATH.search(line)
            if m and not _PLACEHOLDER.search(line):
                offences.append(
                    f"{path.relative_to(ROOT).as_posix()}:{i}: home directory of "
                    f"{m.group(1)!r} in a published file"
                )
    fail("published file(s) carrying a home directory path", offences)


# --------------------------------------------------------------------------------
# Reports that must be complete, because they read as complete
# --------------------------------------------------------------------------------


def _function(name: str) -> ast.FunctionDef:
    """One top-level function of ``server.py``, by name."""
    for node in ast.walk(ast.parse(read(SERVER))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in server.py")


def _strings_in(node: ast.AST) -> set[str]:
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_delete_track_measures_both_places_a_clip_can_live() -> None:
    """A confirmation step that omits half the losses is worse than none.

    Live keeps clips in two independent places: the Session grid and the
    Arrangement timeline. ``delete_track``'s message says the track goes "with its
    devices, its clips and its clip automation", a sentence that reads as a
    complete inventory, so the dry run has to measure both places.
    """
    strings = _strings_in(_function("delete_track"))
    missing = [
        row
        for row in ("clip_slot.has_clip", "arrangement_clip.name")
        if row not in strings
    ]
    fail("delete_track no longer measures", [f"{row}: a clip can live there" for row in missing])


def test_no_tool_reports_an_unqualified_clip_count() -> None:
    """``clip_count`` is a total, and this server never surveys the Arrangement.

    The snapshot sweeps Session slots. Reporting that as ``clip_count`` invites the
    reader to treat it as every clip in the set, which it is not once the Arrangement
    holds material. The field is ``session_clip_count`` and the qualifier is the whole
    point of it.
    """
    offences = []
    for path in tracked_files(".py"):
        if path.name == Path(__file__).name:
            continue
        for i, line in enumerate(read(path).splitlines(), 1):
            if re.search(r'"clip_count"|\bsnap\.clip_count\b', line):
                offences.append(
                    f"{path.relative_to(ROOT).as_posix()}:{i}: unqualified clip_count: "
                    "say session_clip_count, or count the Arrangement too"
                )
    fail("unqualified clip count(s)", offences)


def test_read_clip_notes_can_be_asked_a_narrow_question() -> None:
    """Reading one bar must not mean transferring the clip.

    A single 384-note clip comes back as 57k characters, past the tool-result cap,
    and a survey of many clips reads each one in full. Live has taken a time window
    since Live 9 and the LOM has no ``note_count`` at all, so both of these are ours
    to offer.
    """
    arguments = {arg.arg for arg in _function("read_clip_notes").args.args}
    missing = sorted({"from_time", "time_span", "count_only"} - arguments)
    fail("read_clip_notes can no longer be asked for less than everything", missing)


def test_a_group_tracks_loss_report_does_not_read_as_empty() -> None:
    """A group holds no clips of its own, and four tracks.

    ``delete_track`` on a group answered "0 device(s), 0 Session clip(s), no
    Arrangement clip(s)": every count honest, the whole reading false, because all of
    them describe the container. The fixture below is a real reading from Live 12.4.5:
    group "Vocal Bridge" at index 28, tracks 29-32 inside it, track 33 outside.
    """
    from live_maestro import server

    measured = {"is_grouped": {"values": [False] * 29 + [True] * 4 + [False] * 18}}
    tracks = {
        "values": [f"track {i}" for i in range(29)]
        + [
            "29 Bridge Lead (Voice 0)",
            "30 Bridge Double (Voice 1)",
            "31 Bridge HarmLow (Voice 2)",
            "32 Bridge HarmHigh (Voice 3)",
        ]
        + [f"track {i}" for i in range(33, 51)]
    }
    members = server._members_of_group(28, measured, tracks)
    assert [m["index"] for m in members] == [29, 30, 31, 32], members
    assert members[0]["name"] == "29 Bridge Lead (Voice 0)"

    warning = server._group_warning({"is_group": True, "contained_tracks": members})
    for owed in ("group track", "container", "4 track(s)", "was not measured"):
        assert owed in warning, f"{owed!r} missing from the group warning: {warning}"

    # An ordinary track must not acquire the warning.
    assert server._group_warning({"is_group": False}) == ""


def test_load_device_says_what_aims_a_load_and_what_does_not() -> None:
    """A tool that cannot be aimed owes the caller the truth about that.

    ``Browser.load_item`` takes no target and follows Live's selection.
    ``song.view.selected_track`` moves that selection and the docstring must name it.

    ``rack.view.selected_chain`` does not. On Live 12.4.5 the write reads back as the
    chain that was asked for, and a device loaded afterwards still lands on the track,
    leaving the chain's device count untouched. The docstring has to say so, because a
    caller who believes it can aim into a rack will replace an instrument instead
    (docs/limits.md section 2).
    """
    doc = ast.get_docstring(_function("load_device")) or ""
    offences = []
    if "selected_track" not in doc:
        offences.append("selected_track: the one selection a load does follow")
    if "selected_chain" not in doc:
        offences.append("selected_chain: the caller has to be told it does not aim a load")
    elif "cannot be aimed inside a rack" not in doc:
        offences.append(
            "selected_chain is named without saying that it does not aim a load"
        )
    fail("load_device no longer says what aims a load", offences)


def test_every_read_only_handler_exists_in_the_script() -> None:
    """``READ_ONLY_HANDLERS`` must not name a handler the script does not have.

    Which handlers belong in that set is asserted in ``tests/test_client.py``,
    beside the client that uses it. This checks only the half that file cannot,
    namely that every name in it is a handler the Remote Script actually declares. A set
    naming a handler that does not exist gives a timeout class and a retry to
    nothing, and nothing complains.
    """
    from live_maestro.client import READ_ONLY_HANDLERS

    declared = script_handlers()
    fail(
        "handler(s) in READ_ONLY_HANDLERS that the Remote Script does not declare",
        sorted(set(READ_ONLY_HANDLERS) - declared),
    )


def script_handlers() -> set[str]:
    """The ``HANDLERS`` tuple the Remote Script declares, read off its AST."""
    for node in ast.walk(ast.parse(read(SCRIPT))):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "HANDLERS" for t in node.targets
        ):
            return {ast.literal_eval(e) for e in _literal_elements(node.value)}
    raise AssertionError("HANDLERS not found in the Remote Script")


# --------------------------------------------------------------------------------
# A document does not narrate its own edit history
# --------------------------------------------------------------------------------

#: The self-reference, not the correction. "Widely assumed impossible; it is not,
#: and here is the measurement" is exactly what this repository is for. "This
#: section used to say X, and that was wrong" is a different sentence: it tells a
#: reader about a revision they never saw, it dates instantly, and the place for
#: it is the commit message that made the change.
#: Prose about this repository's own files and past, which no exemption covers. A test
#: may name the defect it guards; it may not name the file that used to hold it.
_REPOSITORY_NARRATIVE = re.compile(
    r"\bused\s+to\s+live\s+in\b"
    r"|\breplaces?\s+the\s+(?:pair|tests?|fake|module|helper)\s+(?:that|which|above)\b"
    r"|\bthe\s+old\s+(?:fake|test|version|code|module)\b"
    r"|\bthe\s+first\s+test\s+in\s+this\s+repository\b"
    r"|\bdefects?\s+shipped\b"
    r"|\bwhy\s+this\s+module\s+exists\b",
    re.IGNORECASE,
)

_EDIT_HISTORY = re.compile(
    r"\b(?:this|the)\s+(?:row|docstring|document|section|module|page|file|paragraph|line|"
    r"list|catalog|project)\s+(?:used\s+to|said|claimed|briefly|got\s+(?:it\s+)?wrong|"
    r"had\s+it\s+wrong)"
    r"|\ban\s+earlier\s+version\s+of\s+(?:this|the)\b"
    r"|\bthe\s+first\s+version\s+of\s+this\b"
    r"|\buntil\s+it\s+was\s+measured\b"
    r"|\bsaid\s+the\s+opposite\b",
    re.IGNORECASE,
)

#: Inline code is a quotation, not a claim: CONTRIBUTING.md has to show the bad
#: form to forbid it.
_INLINE_CODE = re.compile(r"`[^`]*`|``[^`]*``")


def test_no_shipped_prose_narrates_its_own_edit_history() -> None:
    """Nobody reading this repository has seen an earlier draft of it.

    A retraction is worth keeping: somebody planned around a limit and needs to
    learn it was lifted. What that wants is the claim named as an assumption, not
    the file's revision history: "widely assumed impossible; it is not, and here is
    the measurement". The history goes in the commit that made the change, where it
    is exact and where it does not age into a puzzle.

    ``tests/`` may name the defect a test guards, which is the test's purpose. It may
    not narrate this repository: a file that used to hold these tests, a fake that was
    replaced, a sprint the work belonged to. Those patterns are in
    :data:`_REPOSITORY_NARRATIVE` and apply everywhere.
    """
    offences = []
    for path in tracked_files(".md", ".py", ".yaml", ".yml"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == Path(__file__).relative_to(ROOT).as_posix():
            continue
        for i, line in enumerate(read(path).splitlines(), 1):
            hit = _REPOSITORY_NARRATIVE.search(_INLINE_CODE.sub("", line))
            if hit:
                offences.append(f"{relative}:{i}: {hit.group(0)!r} (narrates the repository)")
        if relative.startswith("tests/"):
            continue
        for i, line in enumerate(read(path).splitlines(), 1):
            match = _EDIT_HISTORY.search(_INLINE_CODE.sub("", line))
            if match:
                offences.append(f"{relative}:{i}: {match.group(0)!r} (write about the claim)")
    fail("prose narrating its own edit history", offences)


# --------------------------------------------------------------------------------
# Version numbers
# --------------------------------------------------------------------------------

#: A version literal in a line that is talking about the Remote Script. Live versions and
#: loopback addresses are three-part numbers too, so the subject of the sentence
#: decides, not the shape of the number.
#: The trailing half is a lookahead so that the match consumes the number alone. A
#: consuming form swallows the word it matched on, and the provenance sentence carries
#: both numbers: a match running from the Live version through the word "Script" leaves
#: the script version behind the scan head, where nothing looks at it again.
_SCRIPT_VERSION_CLAIM = re.compile(
    r"(?i)\bscript\b[^\n]{0,60}?(\d+\.\d+\.\d+)"
    r"|(\d+\.\d+\.\d+)(?=[^\n]{0,20}?\bscript\b)"
)

#: Files whose version literals are not claims: the package's own metadata, test
#: fixtures where a fake reply only has to round-trip, and the one module that
#: defines SCRIPT_VERSION in the first place.
_VERSION_EXEMPT = (
    "pyproject.toml",
    "src/live_maestro/__init__.py",
    "tests/",
    "live-remote-script/__init__.py",
)


def script_version() -> str:
    """``SCRIPT_VERSION`` from the Remote Script, read off its AST."""
    for node in ast.walk(ast.parse(read(SCRIPT))):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "SCRIPT_VERSION" for t in node.targets
        ):
            return str(ast.literal_eval(node.value))
    raise AssertionError("SCRIPT_VERSION not found in the Remote Script")


def test_no_prose_cites_a_remote_script_version() -> None:
    """A measurement is dated against a Live version, never against a script one.

    Three version numbers live here and they move independently: the Python
    package, ``SCRIPT_VERSION`` in the Remote Script, and ``PROTOCOL_VERSION``.
    Only the Live version belongs in a measurement note, because it is the thing
    being measured.

    A script version in prose is wrong in one of two ways and usually both. It
    ages on the next release, which is why the protocol examples hold a
    placeholder. And it is often unmeasurable after the fact: the script version a
    measurement ran under is not recorded anywhere the note can be checked
    against. The check is on the sentence shape rather than on a list of numbers.
    """
    current = script_version()
    offences = []
    for path in tracked_files(".md", ".py", ".yaml", ".yml", ".toml"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == Path(__file__).name or relative.startswith(_VERSION_EXEMPT):
            continue
        lines = read(path).splitlines()
        for i, line in enumerate(lines, 1):
            for match in _SCRIPT_VERSION_CLAIM.finditer(line):
                found = match.group(1) or match.group(2)
                if found == current:
                    continue
                # "the Remote Script ... measured against Live 12.4.5" puts both
                # words in one line. What the number belongs to is decided by what
                # stands immediately before it, and a wrapped paragraph can put
                # that word on the line above, which is why the previous line is
                # part of the lookback.
                at = line.index(found, match.start())
                before = (lines[i - 2] if i >= 2 else "") + " " + line[:at]
                if "live" in before[-12:].casefold():
                    continue
                offences.append(
                    f"{relative}:{i}: cites script version {found!r}; the script is "
                    f"{current} and a measurement is dated against Live"
                )
    fail("script version(s) cited in prose", offences)


def test_the_package_version_is_stated_once_and_agrees_with_itself() -> None:
    """Three literals, one number. The only grouping here that must be kept in step.

    ``pyproject.toml``, ``live_maestro.__version__`` and ``SCRIPT_VERSION`` in the
    Remote Script all carry it, and one number written in three places drifts unless
    something compares them.

    The Remote Script is in the group on purpose. It cannot import the package, so it
    holds a literal, and keeping that literal equal is diagnostic: the script inside
    Live only reloads when Live restarts, so a handshake reporting a number below the
    installed package says the script is stale and nothing else. What the number
    cannot do is prove the file is current, because an edit without a bump looks
    identical, and ``python scripts/install_script.py --diff`` compares the bytes.

    A third number is not checked here: ``importlib.metadata.version`` reports
    whatever was last installed, which lags the source until the package is
    reinstalled, and nothing in this repository reads it.
    """
    import tomllib

    declared = tomllib.loads(read(ROOT / "pyproject.toml"))["project"]["version"]
    module = next(
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(read(ROOT / "src" / "live_maestro" / "__init__.py")))
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "__version__" for t in node.targets)
    )
    assert declared == module, (
        f"pyproject.toml says {declared!r} and __init__.py says {module!r}. "
        "One number, two places, and they have drifted."
    )
    assert declared == script_version(), (
        f"pyproject.toml says {declared!r} and the Remote Script's SCRIPT_VERSION says "
        f"{script_version()!r}. They are one number: a script reporting less than the "
        "package has to mean 'Live was not restarted', and it cannot mean that while "
        "the two are allowed to differ on purpose."
    )


# --------------------------------------------------------------------------------
# The Remote Script, which nothing else checks
# --------------------------------------------------------------------------------

#: Everything the Remote Script is allowed to import: the standard library of the
#: interpreter running these tests, plus what Live itself provides. ``Queue`` is
#: the Python 2 spelling and is not a 3.x stdlib name. The script keeps it behind
#: a ``try`` because Live's interpreter history is long.
_SCRIPT_MAY_IMPORT = {"Live", "_Framework", "Queue"}


def _script_tree() -> ast.AST:
    return ast.parse(read(SCRIPT))


def test_the_remote_script_compiles() -> None:
    """The one file in this repository that no other check would catch.

    ``ruff`` excludes ``live-remote-script/`` (it is not Python 3.11 code) and no
    test imports it, because importing it needs Live's own ``Live`` and
    ``_Framework`` modules. So a syntax error here survives the whole suite and
    surfaces as Live failing to load the Control Surface, after an install, a
    ``__pycache__`` delete and a restart.

    ``compile`` rather than ``ast.parse``: a ``continue`` outside a loop parses
    cleanly and only fails at compile time, which is exactly the kind of edit a
    prose sweep can leave behind.
    """
    try:
        compile(read(SCRIPT), str(SCRIPT), "exec")
    except SyntaxError as exc:
        pytest.fail(f"{SCRIPT.name}:{exc.lineno}: {exc.msg}")


def test_the_remote_script_avoids_syntax_lives_interpreter_may_not_have() -> None:
    """No f-strings and no annotations, because this file is imported by Live.

    The script says so itself: "no type annotations, no f-strings -- the
    interpreter is Live's, and its history is long". Every other file in the
    repository is Python 3.11+ with full type hints, so both are what a reformatter
    reaches for by reflex, and neither is caught by anything else. This file compiles
    fine under 3.12 and would still be refused by an older embedded interpreter.
    """
    offences = []
    for node in ast.walk(_script_tree()):
        if isinstance(node, ast.JoinedStr):
            offences.append(f"line {node.lineno}: f-string")
        elif isinstance(node, ast.AnnAssign):
            offences.append(f"line {node.lineno}: annotated assignment")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                offences.append(f"line {node.lineno}: return annotation on {node.name}()")
            args = node.args
            for arg in args.posonlyargs + args.args + args.kwonlyargs:
                if arg.annotation is not None:
                    offences.append(
                        f"line {node.lineno}: annotation on {node.name}() argument {arg.arg}"
                    )
    fail("syntax in the Remote Script that Live's interpreter may refuse", offences)


def test_the_remote_script_imports_only_what_live_provides() -> None:
    """Standard library plus Live's own modules. No third-party dependency, ever.

    The script is installed into the user's Ableton Live folder as a single file. There
    is no place to put a dependency and no installer to fetch one, so an import
    outside this set is not a bad choice, it is a file that cannot load.
    """
    allowed = set(sys.stdlib_module_names) | _SCRIPT_MAY_IMPORT
    offences = []
    for node in ast.walk(_script_tree()):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top and top not in allowed:
                offences.append(f"line {node.lineno}: imports {name!r}, which Live will not have")
    fail("import(s) the Remote Script cannot rely on", offences)
