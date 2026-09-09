"""The prose standard for docstrings and comments.

These checks cover formatting and scope rules:
- PEP 8 punctuation standard: docstrings and comments avoid typographical em-dashes (—).
- Live measurement scope: dated Live measurements belong only in modules that can observe Live.
"""

from __future__ import annotations

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
)

DATED = re.compile(r"[Mm]easured 20\d{2}-\d{2}-\d{2}")


def python_files() -> list[Path]:
    """Return every Python file under src/ and scripts/, caches excluded."""
    out: list[Path] = []
    for root in ("src", "scripts"):
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
        if DATED.search(read_source(path)) and not module.startswith(LIVE_FACING):
            offences.append(f"{module}: dates a finding against Live but never reaches it")
    fail("Live-dated measurement outside a Live-facing module", offences)


def read_source(path: Path) -> str:
    """Return the text of a Python file."""
    return path.read_text(encoding="utf-8")
