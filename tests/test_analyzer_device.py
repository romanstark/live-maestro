"""What the analyzer device measures, checked in the file it is committed as.

The properties every generated device shares are in ``tests/test_devices.py``, which
runs them once per registered device: the container bytes, the reviewable ``.maxpat``
twin, the object names, the graph connectivity, the cord indices, the OSC contract and
the reply slots. This file holds what is true of the analyzer alone, because it is the
device that measures loudness, phase and band energy: the filter pairs at the declared
band edges, one averaging pair per averaged field, the averaging ceiling, and an audio
path that passes through untouched.

Neither file needs Max. Both read the committed ``.amxd``, which is what Live loads.
"""

from __future__ import annotations

import collections

from test_devices import ANALYZER, graph_of, patcher_of, text_of, texts_of

from live_maestro.analyzer import protocol


def test_the_loudness_object_is_the_one_that_exists() -> None:
    """``ebur128~`` is not a Max object. ``loudness~`` reports EBU R128 loudness."""
    heads = {text.split()[0] for text in texts_of(patcher_of(ANALYZER))}
    assert "loudness~" in heads
    assert "ebur128~" not in heads


def test_each_band_edge_is_two_sections_at_the_declared_frequency() -> None:
    filters = collections.Counter(
        text for text in texts_of(patcher_of(ANALYZER)) if text.startswith("svf~ ")
    )
    for _, low, high in protocol.BANDS:
        assert filters[f"svf~ {low:g} 0"] >= 2, f"no high pass pair at {low} Hz"
        if high is not None:
            assert filters[f"svf~ {high:g} 0"] >= 2, f"no low pass pair at {high} Hz"
    assert filters[f"svf~ {protocol.LOW_BAND_HZ:g} 0"] == 4, (
        "the low band correlation needs two sections on each of the two channels"
    )


def test_one_average_and_one_snapshot_per_averaged_field() -> None:
    patcher = patcher_of(ANALYZER)
    graph = graph_of(patcher)
    heads = collections.Counter(text.split()[0] for text in texts_of(patcher))
    # Three broadband figures, three below 120 Hz, one per band.
    expected = 3 + 3 + len(protocol.BANDS)
    assert heads["average~"] == expected
    averaging_pairs = [line for line in patcher["lines"]
                       if text_of(graph.boxes[line["patchline"]["source"][0]]).startswith(
                           "average~")
                       and text_of(graph.boxes[line["patchline"]["destination"][0]]) == "snapshot~"]
    assert len(averaging_pairs) == expected


def test_every_average_holds_the_declared_ceiling() -> None:
    for text in texts_of(patcher_of(ANALYZER)):
        if text.startswith("average~ "):
            assert text.split()[1] == str(protocol.MAX_AVERAGING_SAMPLES)


def test_the_audio_passes_through_untouched() -> None:
    """A meter that colours the signal is not a meter."""
    patcher = patcher_of(ANALYZER)
    graph = graph_of(patcher)
    source = next(i for i, b in graph.boxes.items() if text_of(b).startswith("plugin~"))
    sink = next(i for i, b in graph.boxes.items() if text_of(b).startswith("plugout~"))
    cords = {
        (line["patchline"]["source"][1], line["patchline"]["destination"][1])
        for line in patcher["lines"]
        if line["patchline"]["source"][0] == source
        and line["patchline"]["destination"][0] == sink
    }
    assert cords == {(0, 0), (1, 1)}, "left to left and right to right, nothing between"
    # Nothing else may reach the output: a cord from anywhere but plugin~ would be
    # this device changing what it is only supposed to measure.
    feeding_output = {
        line["patchline"]["source"][0]
        for line in patcher["lines"]
        if line["patchline"]["destination"][0] == sink
    }
    assert feeding_output == {source}
