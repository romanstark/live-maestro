"""When a fired clip or scene will actually start.

Live quantises a launch to the next grid boundary, so ``fire`` starts nothing at the moment
it is called and reports nothing about when it will: it answers that it succeeded either
way. Aiming a scene at a particular bar from outside is therefore a race, and a launch that
misses its boundary lands on the next one with nothing in the result to show it.

This module does the arithmetic that makes the landing visible. It takes the grid Live
is set to and a position in beats and answers which beat the launch falls on. It reads
nothing and calls nothing.

A launch is predictable only to the width of the window it was issued in, so where
that window spans a boundary the landing is one of two beats. :func:`landing` reports
both rather than choosing.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "QUANTIZATION_BEATS",
    "QUANTIZATION_NAMES",
    "Landing",
    "bar_beats",
    "grid_beats",
    "landing",
]

#: Live's ``Quantization`` enum as a length in quarter notes, or None where the value
#: is a number of bars and the bar length has to come from the time signature.
#: Index 0 is off, and a launch under it starts at once rather than on a grid.
QUANTIZATION_BEATS: dict[int, float | None] = {
    0: 0.0,
    1: None,  # 8 bars
    2: None,  # 4 bars
    3: None,  # 2 bars
    4: None,  # 1 bar
    5: 2.0,
    6: 4.0 / 3.0,
    7: 1.0,
    8: 2.0 / 3.0,
    9: 0.5,
    10: 1.0 / 3.0,
    11: 0.25,
    12: 1.0 / 6.0,
    13: 0.125,
}

#: How many bars the bar-relative values mean.
_BARS: dict[int, int] = {1: 8, 2: 4, 3: 2, 4: 1}

QUANTIZATION_NAMES: dict[int, str] = {
    0: "none", 1: "8 bars", 2: "4 bars", 3: "2 bars", 4: "1 bar",
    5: "1/2", 6: "1/2 triplet", 7: "1/4", 8: "1/4 triplet", 9: "1/8",
    10: "1/8 triplet", 11: "1/16", 12: "1/16 triplet", 13: "1/32",
}


def bar_beats(numerator: int = 4, denominator: int = 4) -> float:
    """How many quarter notes one bar holds at this time signature.

    >>> bar_beats(4, 4)
    4.0
    >>> bar_beats(3, 4)
    3.0
    >>> bar_beats(6, 8)
    3.0
    """
    if numerator <= 0 or denominator <= 0:
        raise ValueError(f"time signature {numerator}/{denominator} is not usable")
    return numerator * (4.0 / denominator)


def grid_beats(quantization: int, numerator: int = 4, denominator: int = 4) -> float:
    """The launch grid as a length in quarter notes. 0.0 means no quantisation.

    Raises:
        ValueError: The index is not one of Live's fourteen.

    >>> grid_beats(4)
    4.0
    >>> grid_beats(1)
    32.0
    >>> grid_beats(7)
    1.0
    >>> grid_beats(0)
    0.0
    >>> grid_beats(4, 3, 4)
    3.0
    """
    if quantization not in QUANTIZATION_BEATS:
        raise ValueError(
            f"{quantization} is not a Live launch quantisation: "
            f"{min(QUANTIZATION_BEATS)} to {max(QUANTIZATION_BEATS)}"
        )
    fixed = QUANTIZATION_BEATS[quantization]
    if fixed is not None:
        return fixed
    return _BARS[quantization] * bar_beats(numerator, denominator)


@dataclass(frozen=True)
class Landing:
    """Where a launch issued inside one window will start.

    Attributes:
        grid_beats: The launch grid, 0.0 where quantisation is off.
        earliest: The beat a launch issued at the start of the window lands on.
        latest: The beat a launch issued at the end of the window lands on. Equal to
            ``earliest`` unless the window crossed a boundary.
        certain: True when both ends agree, so the landing beat is known.
        window_beats: How wide the window was.
    """

    grid_beats: float
    earliest: float
    latest: float
    certain: bool
    window_beats: float


def _next_boundary(position: float, grid: float) -> float:
    """The first grid boundary at or after ``position``."""
    if grid <= 0.0:
        return position
    steps = position / grid
    whole = int(steps)
    if steps - whole < 1e-9:
        return whole * grid
    return (whole + 1) * grid


def landing(
    before: float, after: float, quantization: int, numerator: int = 4,
    denominator: int = 4,
) -> Landing:
    """Where a launch issued between two readings of the song position will start.

    Args:
        before: Song position in beats, read before firing.
        after: Song position in beats, read after firing.
        quantization: Live's ``song.clip_trigger_quantization``.
        numerator: Time signature numerator, for the bar-relative grids.
        denominator: Time signature denominator.

    Returns:
        A Landing. With quantisation off the launch starts at the moment it was fired,
        so the window's own ends are the answer and the beat is known no better than
        the window is wide.

    >>> at_bar = landing(55.6, 55.7, 4)
    >>> at_bar.earliest, at_bar.latest, at_bar.certain
    (56.0, 56.0, True)
    >>> across = landing(63.9, 64.1, 4)
    >>> across.earliest, across.latest, across.certain
    (64.0, 68.0, False)
    >>> off = landing(55.6, 55.7, 0)
    >>> (off.earliest, off.latest, off.grid_beats)
    (55.6, 55.7, 0.0)
    >>> stopped = landing(0.0, 0.0, 4)
    >>> stopped.earliest, stopped.certain
    (0.0, True)
    """
    grid = grid_beats(quantization, numerator, denominator)
    low, high = (before, after) if after >= before else (after, before)
    earliest = _next_boundary(low, grid)
    latest = _next_boundary(high, grid)
    return Landing(
        grid_beats=grid,
        earliest=earliest,
        latest=latest,
        certain=abs(earliest - latest) < 1e-9,
        window_beats=round(high - low, 6),
    )
