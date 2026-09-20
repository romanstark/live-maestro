"""The prose standard for docstrings and comments.

These checks cover formatting and scope rules:
- PEP 8 punctuation standard: docstrings and comments avoid typographical em-dashes (—).
- Live measurement scope: dated Live measurements belong only in modules that can observe Live.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EM_DASH = "—"

#: Modules allowed to date a finding against a Live version. Each one either talks to
#: Live over the socket, reads or writes the saved project file, reads Live's own
#: installation on disk, or ships the catalog rows those measurements are written onto.
#: A module outside this set claiming a Live measurement is describing something it
#: cannot observe.
LIVE_FACING = (
    "src/live_maestro/installer.py",
    "src/live_maestro/server.py",
    "src/live_maestro/vocabulary.py",
    "src/live_maestro/client.py",
    "src/live_maestro/executor.py",
    "src/live_maestro/models.py",
    "src/live_maestro/registry.py",
    "src/live_maestro/lom/",
    "src/live_maestro/als/",
    "src/live_maestro/music/notes.py",
    "src/live_maestro/music/theory.py",
    "src/live_maestro/music/humanize.py",
    "src/live_maestro/automation.py",
    "scripts/",
    "live-remote-script/",
    # A test that pins a value Live produced states where the value came from. That is
    # provenance for a fixture, not a claim about Live made by code that cannot see it.
    "tests/",
)

DATED = re.compile(r"[Mm]easured 20\d{2}-\d{2}-\d{2}")


def python_files() -> list[Path]:
    """Return every Python file this repository publishes, caches excluded.

    tests/ and live-remote-script/ are in the set: both ship and both are read.
    """
    out: list[Path] = []
    for root in ("src", "scripts", "tests", "live-remote-script"):
        out.extend(
            p for p in sorted((ROOT / root).rglob("*.py")) if "__pycache__" not in p.parts
        )
    return out


def relative(path: Path) -> str:
    """Return the repository-relative POSIX path of a file."""
    return path.relative_to(ROOT).as_posix()


def prose_tokens(source: str) -> list[tuple[int, str]]:
    """Return (line, text) for every docstring and comment token in source.

    A string literal that is not a docstring is excluded: it is runtime output or a
    pattern, not prose about the code.
    """
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    skip = (
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.COMMENT,
        tokenize.ENCODING,
    )
    out: list[tuple[int, str]] = []
    for i, token in enumerate(tokens):
        if token.type == tokenize.COMMENT:
            out.append((token.start[0], token.string))
            continue
        if token.type != tokenize.STRING:
            continue
        back = i - 1
        while back >= 0 and tokens[back].type in skip:
            back -= 1
        is_docstring = back < 0 or (
            tokens[back].type == tokenize.OP and tokens[back].string == ":"
        )
        if is_docstring:
            out.append((token.start[0], token.string))
    return out


def fail(headline: str, offences: list[str]) -> None:
    """Fail with one line per offence, or return when there are none."""
    if offences:
        body = "\n".join(f"  - {line}" for line in offences)
        pytest.fail(f"{headline}:\n{body}", pytrace=False)


def test_no_docstring_or_comment_uses_an_em_dash() -> None:
    """An em-dash in prose is a hyphen, comma, colon or pair of parentheses.

    Scoped to docstrings and comments. The three em-dashes this repository needs are
    all inside patterns, where the character is matched rather than read.
    """
    offences = []
    for path in python_files():
        if path.name == Path(__file__).name:
            continue
        for line, text in prose_tokens(read_source(path)):
            if EM_DASH in text:
                offences.append(f"{relative(path)}:{line}")
    fail("em-dash in a docstring or comment", offences)


def test_a_live_dated_measurement_sits_in_a_module_that_reaches_live() -> None:
    """``Measured <date> against Live <version>`` belongs where Live can be observed.

    The form is this project's evidence that a claim was checked rather than assumed.
    Applied to something no Live version has any bearing on, it spends that credibility
    on a guess.
    """
    offences = []
    for path in python_files():
        module = relative(path)
        if module.startswith(LIVE_FACING) or path.name == Path(__file__).name:
            continue
        for line, text in prose_tokens(read_source(path)):
            if DATED.search(text):
                offences.append(
                    f"{module}:{line}: dates a finding against Live but never reaches it"
                )
    fail("Live-dated measurement outside a Live-facing module", offences)


def read_source(path: Path) -> str:
    """Return the text of a Python file."""
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------
# Length. The only rule here that cannot be written around.
# --------------------------------------------------------------------------------

#: Lines a module docstring may spend on prose. Taken from this repository's own
#: earlier files, whose module docstrings run to a median of twelve lines: a summary
#: line, a blank, and a paragraph of what the module does and where it stops. The cap
#: exists because every other rule in this file names a phrase, and a phrase can be
#: avoided while the essay it belonged to stays. A line count cannot be avoided.
MODULE_DOCSTRING_LINES = 15

#: Lines a function, method or class docstring may spend on prose. Structured sections
#: do not count: Args grows with the signature and Returns with the shape, and neither
#: is where narrative collects. What is counted is the summary, the free paragraphs and
#: the Note section, which is where it does.
PROSE_DOCSTRING_LINES = 25

#: Section headings whose contents are structure rather than prose. Args grows with the
#: signature, Usage with the command line, and neither can hide an essay. Note: is
#: deliberately absent: that is where narrative collects, so it counts.
STRUCTURED_SECTIONS = (
    "Args:", "Returns:", "Raises:", "Yields:", "Attributes:",
    "Usage:", "Usage::", "Example:", "Examples:",
)


def prose_lines(docstring: str) -> list[str]:
    """Return the docstring's free prose, without structured sections or doctests.

    A structured section runs from its heading until a line comes back to the heading's
    own indentation, which is what the next paragraph or heading does.
    """
    out: list[str] = []
    section_indent: int | None = None
    for raw in docstring.split("\n"):
        line = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if section_indent is not None:
            if not line or indent > section_indent:
                continue
            section_indent = None
        if line in STRUCTURED_SECTIONS:
            section_indent = indent
            continue
        if line.startswith((">>>", "...")):
            continue
        if line:
            out.append(line)
    return out


def docstrings_of(source: str) -> list[tuple[int, str, str]]:
    """Return (line, what, docstring) for the module and every def and class in it."""
    tree = ast.parse(source)
    found: list[tuple[int, str, str]] = []
    module = ast.get_docstring(tree, clean=False)
    if module:
        found.append((1, "module", module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                found.append((node.lineno, node.name, doc))
    return found


def test_a_module_docstring_says_what_the_module_is_and_stops() -> None:
    """A module docstring is an orientation, not an essay about its own making."""
    offences = []
    for path in python_files():
        for line, what, doc in docstrings_of(read_source(path)):
            if what != "module":
                continue
            body = prose_lines(doc)
            if len(body) > MODULE_DOCSTRING_LINES:
                offences.append(
                    f"{relative(path)}:{line}: module docstring spends "
                    f"{len(body)} lines of prose, the cap is {MODULE_DOCSTRING_LINES}"
                )
    fail("module docstring(s) over the prose cap", offences)


def test_a_docstring_does_not_grow_an_essay_around_its_signature() -> None:
    """Args and Returns may grow with the code. The prose around them may not."""
    offences = []
    for path in python_files():
        for line, what, doc in docstrings_of(read_source(path)):
            if what == "module":
                continue
            body = prose_lines(doc)
            if len(body) > PROSE_DOCSTRING_LINES:
                offences.append(
                    f"{relative(path)}:{line}: {what} spends {len(body)} lines of "
                    f"prose outside Args/Returns, the cap is {PROSE_DOCSTRING_LINES}"
                )
    fail("docstring(s) over the prose cap", offences)


#: Labels from this project's own planning. A reader outside the repository cannot
#: resolve them and a reader inside does not need them in the source: what a test guards
#: is the behaviour it asserts, not the sprint it was written in.
INTERNAL_LABEL = re.compile(
    r"\bPhase\s+(?:[0-9]|[A-E])\b"
    r"|\bSpike\s+[0-9]\b"
    r"|\b[A-E][0-9]\s+acceptance\b",
)


def test_no_prose_names_an_internal_phase_or_ticket() -> None:
    """Planning labels do not survive into the source."""
    offences = []
    for path in python_files():
        if path.name == Path(__file__).name:
            continue
        for line, text in prose_tokens(read_source(path)):
            found = INTERNAL_LABEL.search(text)
            if found:
                offences.append(f"{relative(path)}:{line}: names {found.group(0)!r}")
    fail("internal planning label in shipped prose", offences)
