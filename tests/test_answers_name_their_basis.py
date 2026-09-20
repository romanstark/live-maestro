"""An answer says what it was taken from: band occupancy, transport, file identity.

Occupancy is a share of time: a continuous tone and the same tone at a 1-in-8 duty
cycle, normalised to the same mean, report the same spectrum and occupancies a factor
of eight apart, and a read that has counted no window reports no measurement rather
than a fresh one dressed as an accumulation. A transport reading that could not be
taken is never shaped like one that was: ``_query_transport`` answers with the state
and no reason, or with no state and a machine code beside one sentence saying what
stopped it. Every answer about a file says which file it meant and what that file
looked like at the time, so two answers about the same path can be compared.

Every fake answers in the shape the wire uses: LOM object handles carry ``__lom__``,
``path`` and ``name``, result rows carry ``status`` rather than ``ok``, and the
transport fakes build their rows from the ops they were handed.
"""

from __future__ import annotations

import gzip
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from live_maestro import server
from live_maestro.analyzer.client import Reply
from live_maestro.analyzer.models import TransportState
from live_maestro.analyzer.protocol import BANDS, REPLY_SCHEMA
from live_maestro.client import LiveClient, LiveConnectionError

# ------------------------------------------------------------------ analyzer fakes

#: The device reports its window in samples and the rate it ran at, and the covered
#: span is derived from the two. 19200 samples at 48 kHz is 400 ms.
SAMPLERATE = 48000.0
WINDOW_SAMPLES = 19200.0


def band_reply(
    track_name: str, amplitude: float, window_samples: float = WINDOW_SAMPLES
) -> Reply:
    """One analyzer reply carrying energy in band_high_mid alone.

    A 3 kHz sine lands in the 2 to 6 kHz band, so every other band is at digital
    zero and the two channels sit at the same amplitude. That keeps the signal a
    single-band one, which is what makes the occupancy of that band the only figure
    the duty cycle can move.
    """
    numbers: dict[str, float] = {name: 0.0 for name, _, _ in BANDS}
    numbers.update(
        {
            "schema": float(REPLY_SCHEMA),
            "request_id": 1.0,
            "samplerate": SAMPLERATE,
            "window_samples": window_samples,
            "rms_l": amplitude,
            "rms_r": amplitude,
            "band_high_mid": amplitude,
            "track_id": 7.0,
        }
    )
    return Reply(numbers=numbers, strings={"track_name": track_name})


def accumulator_over(
    track: str,
    amplitudes: Sequence[float],
    *,
    window_samples: float = WINDOW_SAMPLES,
    running_for_s: float | None = None,
) -> server.AudioAccumulator:
    """An accumulator fed one window per amplitude, through the production classifier.

    ``record_sample`` is called directly rather than ``start()``: the background loop
    adds a clock and a socket to a classification that needs neither, and a test that
    sleeps against a sampling interval asserts on how fast the machine it runs on is.

    Args:
        track: The track name the replies carry.
        amplitudes: One band amplitude per window, in the order they arrived.
        window_samples: How many samples each reply says its window held. The device
            clamps the request against its own buffer, so this is what happened while
            ``window_ms`` below is only what was asked for.
        running_for_s: Push the start of the accumulation this far into the past, so
            the elapsed span is a fixed number rather than however long the test took.
    """
    acc = server.AudioAccumulator(track, 400, 12.0, None)
    for amplitude in amplitudes:
        acc.record_sample(band_reply(track, amplitude, window_samples=window_samples))
    if running_for_s is not None:
        acc.started_monotonic = time.monotonic() - running_for_s
    return acc


def silent_analyzer(calls: list[int]) -> type:
    """An AnalyzerClient stand-in that records every measure() and answers with nothing."""

    class _SilentAnalyzerClient:
        async def measure(self, window_ms: int = 400, reset: bool = False) -> list[Reply]:
            calls.append(window_ms)
            return []

    return _SilentAnalyzerClient


def playing_transport() -> TransportState:
    return TransportState(
        is_playing=True, tracks_sounding=1, tracks_total=4, in_transition=False
    )


# ---------------------------------------------------------------------- occupancy


@pytest.mark.anyio
async def test_b1_continuous_and_pulsed_sine_share_a_spectrum_and_differ_eightfold_in_occupancy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same tone, same mean level, one eighth of the time: eighth of the occupancy.

    The two accumulations carry the identical mean band amplitude, so the spectrum has
    to agree, and the pulsed one occupied its band in one window of eight, so the
    occupancy has to differ by a factor of eight. A read that reported the last window
    instead of the counted ones would make the two spectra disagree by about 18 dB.
    """
    measure_calls: list[int] = []
    monkeypatch.setattr(server, "AnalyzerClient", silent_analyzer(measure_calls))
    state = playing_transport()
    monkeypatch.setattr(server, "_query_transport", lambda: (state, None))

    continuous = accumulator_over("Continuous", [0.02] * 8, running_for_s=3.2)
    pulsed = accumulator_over("Pulsed", [0.16] + [0.0] * 7, running_for_s=3.2)
    monkeypatch.setitem(server._ACTIVE_ACCUMULATORS, "Continuous", continuous)
    monkeypatch.setitem(server._ACTIVE_ACCUMULATORS, "Pulsed", pulsed)

    # Pinned on the accumulator itself as well as on the report: the classification and
    # the reporting of it are separate steps and either one can be the broken one.
    assert continuous.total_windows == 8
    assert pulsed.total_windows == 8
    assert continuous.occupied_counts["band_high_mid"] == 8
    assert pulsed.occupied_counts["band_high_mid"] == 1
    assert continuous.band_amplitudes_sum["band_high_mid"] / 8 == pytest.approx(
        pulsed.band_amplitudes_sum["band_high_mid"] / 8, abs=1e-9
    )

    steady = await server.analyze_audio(track="Continuous", accumulate="read")
    burst = await server.analyze_audio(track="Pulsed", accumulate="read")

    steady_occupancy = steady["band_occupancy"]
    burst_occupancy = burst["band_occupancy"]
    assert steady_occupancy["high_mid_pct"] == 100.0
    assert burst_occupancy["high_mid_pct"] == 12.5
    assert steady_occupancy["high_mid_pct"] / burst_occupancy["high_mid_pct"] == 8.0
    for band in ("sub_pct", "bass_pct", "low_mid_pct", "mid_pct", "presence_pct", "air_pct"):
        assert steady_occupancy[band] == 0.0
        assert burst_occupancy[band] == 0.0

    assert steady["spectrum"]["high_mid_dbfs"] == pytest.approx(
        burst["spectrum"]["high_mid_dbfs"], abs=0.1
    )

    for result in (steady, burst):
        assert result["occupancy_threshold_db"] == 12.0
        assert result["accumulated_windows"] == 8
        assert result["band_occupancy_note"] is None
        assert result["accumulation"]["windows"] == 8
        # 19200 samples at 48 kHz is 400 ms, eight of them 3.2 s of audio. The windows
        # reach one window further back than the accumulation has been running, so the
        # stretch they cover is 3.6 s and they fill 3.2 of it.
        assert result["accumulation"]["window_ms"] == 400.0
        assert result["accumulation"]["elapsed_s"] == pytest.approx(3.2, abs=0.05)
        assert result["accumulation"]["coverage_pct"] == pytest.approx(88.9, abs=1.0)

    # A read reports the windows already counted. Taking a fresh measurement here would
    # answer a question about a stretch of time with a sample of the moment of the call.
    assert measure_calls == []


@pytest.mark.anyio
async def test_b1_the_span_is_measured_and_never_covers_more_than_all_of_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """window_ms is what the device did, and coverage is against the stretch reached.

    Two separate claims. The reported window comes from the sample count in the reply
    and not from the window that was asked for, so a device that clamped the request
    against its own buffer is reported as it behaved. And the windows are counted
    against the stretch they reach, which begins one window before the accumulation
    did, so a read taken moments after the first window cannot report more than all
    of the span as covered.
    """
    monkeypatch.setattr(server, "AnalyzerClient", silent_analyzer([]))
    state = playing_transport()
    monkeypatch.setattr(server, "_query_transport", lambda: (state, None))

    # The request was 400 ms; the device answered with half that many samples.
    clamped = accumulator_over("Clamped", [0.02] * 4, window_samples=9600.0, running_for_s=1.0)
    monkeypatch.setitem(server._ACTIVE_ACCUMULATORS, "Clamped", clamped)
    assert clamped.window_ms == 400

    res = await server.analyze_audio(track="Clamped", accumulate="read")

    assert res["accumulation"]["window_ms"] == 200.0
    # 4 x 200 ms of audio, 1.0 s of running plus the first window's own 0.2 s reach.
    assert res["accumulation"]["coverage_pct"] == pytest.approx(66.7, abs=0.5)

    # The first read of all: one window counted and almost no time passed, which is
    # where an unbounded coverage ratio runs away.
    fresh = accumulator_over("Fresh", [0.02], running_for_s=0.2)
    monkeypatch.setitem(server._ACTIVE_ACCUMULATORS, "Fresh", fresh)

    first = await server.analyze_audio(track="Fresh", accumulate="read")

    assert first["accumulation"]["windows"] == 1
    assert first["accumulation"]["window_ms"] == 400.0
    assert first["accumulation"]["coverage_pct"] <= 100.0
    assert first["accumulation"]["coverage_pct"] == pytest.approx(66.7, abs=1.0)


@pytest.mark.anyio
async def test_b1_an_accumulation_is_reached_by_the_argument_that_started_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rename between start and read may not strand the sampling loop.

    The key is the caller's own argument, which is stable for the life of the process.
    Keying on the resolved track name instead lets a rename, or a Channel A read that
    failed and answered None, move the key out from under a running poller: read and
    reset then answer 'not accumulating' while the loop samples on forever.
    """
    named = {"value": "Drums"}

    class RenamingSet:
        def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
            return {"value": named["value"]}

    monkeypatch.setattr(server, "AnalyzerClient", silent_analyzer([]))
    monkeypatch.setattr(server, "_client_instance", RenamingSet)
    monkeypatch.setattr(server, "_query_transport", lambda: (playing_transport(), None))
    monkeypatch.setattr(server, "_ACTIVE_ACCUMULATORS", {})

    started = await server.analyze_audio(track=0, accumulate="start", window_ms=100)
    assert started["ok"] is True
    assert list(server._ACTIVE_ACCUMULATORS) == ["0"]
    running = server._ACTIVE_ACCUMULATORS["0"]
    assert running.match_name == "Drums", "the reply matcher still follows the track name"

    named["value"] = "Bass"
    reset = await server.analyze_audio(track=0, accumulate="reset")

    assert reset["ok"] is True
    assert reset["status"] == "reset"
    assert server._ACTIVE_ACCUMULATORS == {}, "the renamed accumulation was left running"


@pytest.mark.anyio
async def test_b1_reset_beside_a_read_is_documented_where_a_caller_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read reports counted windows, so it asks the device nothing and resets nothing.

    A read that reset the meter would clear an integration the caller is in the middle
    of. Since ``reset`` therefore does nothing on a read, the parameter's own
    description says where it does apply, so a caller following the peak workflow
    while an accumulation runs is not handed an unreset peak in silence.
    """
    measure_calls: list[int] = []
    monkeypatch.setattr(server, "AnalyzerClient", silent_analyzer(measure_calls))
    monkeypatch.setattr(server, "_query_transport", lambda: (playing_transport(), None))
    monkeypatch.setitem(
        server._ACTIVE_ACCUMULATORS, "Counted", accumulator_over("Counted", [0.02] * 2)
    )

    res = await server.analyze_audio(track="Counted", accumulate="read", reset=True)

    assert res["accumulated_windows"] == 2
    assert measure_calls == [], "a read asked the device for a fresh window"

    schema = {tool.name: tool for tool in await server.mcp.list_tools()}["analyze_audio"]
    described = schema.inputSchema["properties"]["reset"]["description"]
    assert "performs no reset" in described
    assert "accumulate" in described
    doc = server.analyze_audio.__doc__ or ""
    assert "reset=true and no accumulate" in doc


@pytest.mark.anyio
async def test_b1_a_reset_that_stopped_nothing_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result never claims an effect it did not have, and a reset is an effect."""
    monkeypatch.setattr(server, "AnalyzerClient", silent_analyzer([]))
    monkeypatch.setattr(server, "_ACTIVE_ACCUMULATORS", {})

    res = await server.analyze_audio(track="NeverStarted", accumulate="reset")

    assert res["ok"] is False
    assert res["code"] == "not_accumulating"
    assert res["status"] == "not_accumulating"
    assert "NeverStarted" in res["error"]
    assert res["error"].endswith(".")
    assert "not_accumulating" not in res["error"]
    assert "summary" not in res
    assert res["transport"] is None
    assert res["transport_unavailable"]["code"] == "not_queried"


@pytest.mark.anyio
async def test_b1_an_accumulator_with_no_windows_reports_no_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read before the first window says so instead of returning a single shot."""
    measure_calls: list[int] = []
    monkeypatch.setattr(server, "AnalyzerClient", silent_analyzer(measure_calls))
    monkeypatch.setitem(
        server._ACTIVE_ACCUMULATORS,
        "Empty",
        server.AudioAccumulator("Empty", 400, 12.0, None),
    )

    res = await server.analyze_audio(track="Empty", accumulate="read")

    assert res["ok"] is False
    assert res["code"] == "no_windows_yet"
    assert res["status"] == "accumulating"
    assert res["accumulated_windows"] == 0
    assert res["band_occupancy"] is None
    assert res["error"].endswith(".")
    assert "no_windows_yet" not in res["error"]
    assert res["transport"] is None
    assert res["transport_unavailable"]["code"] == "not_queried"
    assert measure_calls == []


@pytest.mark.anyio
async def test_b1_start_registers_an_accumulator_and_reset_removes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start, reset and a read after reset, asserted on the registry and not a clock."""
    measure_calls: list[int] = []
    monkeypatch.setattr(server, "AnalyzerClient", silent_analyzer(measure_calls))
    monkeypatch.setattr(server, "_ACTIVE_ACCUMULATORS", {})
    state = playing_transport()
    monkeypatch.setattr(server, "_query_transport", lambda: (state, None))

    start = await server.analyze_audio(track="Drums", accumulate="start", window_ms=100)
    assert start["ok"] is True
    assert start["status"] == "accumulating"
    assert start["transport"]["is_playing"] is True
    assert start["transport_unavailable"] is None
    assert "Drums" in server._ACTIVE_ACCUMULATORS
    assert server._ACTIVE_ACCUMULATORS["Drums"].threshold_db == 12.0

    reset = await server.analyze_audio(track="Drums", accumulate="reset")
    assert reset["ok"] is True
    assert reset["status"] == "reset"
    assert "Drums" not in server._ACTIVE_ACCUMULATORS

    after = await server.analyze_audio(track="Drums", accumulate="read")
    assert after["ok"] is False
    assert after["status"] == "not_accumulating"
    assert after["code"] == "not_accumulating"
    assert after["transport"] is None
    assert after["transport_unavailable"]["code"] == "not_queried"


# ---------------------------------------------------------------------- transport

TRACK_PROP_PATH = re.compile(r"^song\.tracks\[(\d+)\]\.(playing_slot_index|fired_slot_index)$")

#: playing_slot_index across the 15 tracks of Ableton's "Patience" demo song, copied
#: from the 2026-09-16 measurements against Live 12.4.5. The same vector was read while
#: scene 2 was settling, 6 s later once settled, and again after song.stop_playing: the
#: nine tracks reading -2 are exactly the tracks that hold no clip in the fired scene,
#: so -2 is a standing property of the scene and not a moment in a transition.
PATIENCE_PLAYING = (2, 2, 2, 2, -2, -2, -2, -2, 2, -2, -2, -2, -2, -2, 2)

#: The same set with another scene fired from a running transport, before the bar line.
#: playing_slot_index still reads the old scene's slots (the measurement records the set
#: {1, -2}) while fired_slot_index reads 0 or more on nine tracks. The measurement records
#: how many tracks were pending, not which, so which nine carry the fire here is this
#: fake's choice and nothing below depends on the identity of the nine.
PATIENCE_PENDING_PLAYING = (1, 1, 1, 1, -2, -2, -2, -2, 1, -2, -2, -2, -2, -2, 1)
PATIENCE_PENDING_FIRED = (3, 3, 3, 3, 3, 3, 3, 3, 3, -1, -1, -1, -1, -1, -1)


def track_handle(index: int) -> dict[str, Any]:
    """One LOM object handle in the shape the wire carries it.

    The key is ``path``. ``__path__`` is the request key the Remote Script reads and
    never a key it answers with, so a fake that emits ``__path__`` here would be
    testing a reply Live does not send (docs/protocol.md section 7).
    """
    return {"__lom__": "Track", "path": f"song.tracks[{index}]", "name": f"Track {index}"}


def tracks_row_for(count: int, *, listed: int | None = None) -> dict[str, Any]:
    """The ``song.tracks`` row for a set of ``count`` tracks."""
    shown = count if listed is None else listed
    row: dict[str, Any] = {
        "status": "success",
        "path": "song.tracks",
        "type": "list",
        "count": count,
        "class": "Track",
        "value": [track_handle(index) for index in range(shown)],
    }
    if shown < count:
        row["truncated"] = True
    return row


def transport_answer(
    ops: Sequence[Mapping[str, Any]],
    *,
    tracks_row: Mapping[str, Any],
    is_playing: bool = True,
    playing: Sequence[int] | Mapping[int, int] | None = None,
    fired: Sequence[int] | Mapping[int, int] | None = None,
    default_playing: int = -1,
    default_fired: int = -1,
    broken: Mapping[tuple[int, str], Any] | None = None,
) -> list[dict[str, Any]]:
    """Answer one batch from the ops it carried, one row per op and in order.

    A track index past the end of the set answers with a per-op index_out_of_range
    row, which is what lom_batch does with a speculative read rather than aborting
    the whole batch.

    Args:
        ops: The ops the batch carried, answered in order.
        tracks_row: The row the ``song.tracks`` read comes back as.
        is_playing: What the ``song.is_playing`` read answers.
        playing: playing_slot_index per track index, as a sequence or a sparse mapping.
        fired: fired_slot_index per track index, as a sequence or a sparse mapping.
        default_playing: What an index absent from ``playing`` answers.
        default_fired: What an index absent from ``fired`` answers.
        broken: (track index, property name) pairs inside the set whose read fails, each
            mapped to the code it fails with, or to a value of the wrong type. That is a
            per-track read that could not be taken while the set itself is readable,
            which is the case a partial count must not be shipped for.
    """
    total = tracks_row.get("count", 0)

    def lookup(
        table: Sequence[int] | Mapping[int, int] | None, index: int, fallback: int
    ) -> int:
        if table is None:
            return fallback
        if isinstance(table, Mapping):
            return table.get(index, fallback)
        return table[index] if index < len(table) else fallback

    rows: list[dict[str, Any]] = []
    for op in ops:
        path = str(op.get("path", ""))
        if path == "song.is_playing":
            rows.append({"status": "success", "path": path, "type": "bool", "value": is_playing})
            continue
        if path == "song.tracks":
            rows.append(dict(tracks_row))
            continue
        found = TRACK_PROP_PATH.match(path)
        if found is None:
            raise AssertionError(f"the transport read asked for an unexpected path: {path!r}")
        index = int(found.group(1))
        prop = found.group(2)
        if not isinstance(total, int) or index >= total:
            rows.append(
                {
                    "status": "error",
                    "path": path,
                    "code": "index_out_of_range",
                    "message": f"index {index} is out of range",
                }
            )
            continue
        if broken is not None and (index, prop) in broken:
            failure = broken[(index, prop)]
            if isinstance(failure, str):
                rows.append(
                    {
                        "status": "error",
                        "path": path,
                        "code": failure,
                        "message": f"{path} could not be read",
                    }
                )
            else:
                rows.append({"status": "success", "path": path, "type": "int", "value": failure})
            continue
        table = playing if prop == "playing_slot_index" else fired
        fallback = default_playing if prop == "playing_slot_index" else default_fired
        value = lookup(table, index, fallback)
        rows.append({"status": "success", "path": path, "type": "int", "value": value})
    return rows


class RecordingClient(LiveClient):
    """A LiveClient with its socket replaced and nothing else.

    Subclassed rather than written from scratch so the real ``get`` and ``batch``
    wrappers run and every request is seen as the handler name and parameters that go
    on the wire. A fake that answered ``batch`` directly would keep passing on the day
    the transport read starts spending a separate ``lom_get``.
    """

    def __init__(
        self, answer: Callable[[Sequence[Mapping[str, Any]]], list[dict[str, Any]]]
    ) -> None:
        super().__init__(auto_connect=False)
        self.answer = answer
        self.handlers: list[str] = []
        self.batches: list[list[dict[str, Any]]] = []

    def send(
        self,
        handler: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.handlers.append(handler)
        if handler != "lom_batch":
            raise AssertionError(f"the transport read spent a {handler!r} request")
        ops = [dict(op) for op in (params or {}).get("ops", [])]
        self.batches.append(ops)
        rows = self.answer(ops)
        errors = sum(1 for row in rows if row.get("status") == "error")
        return {"results": rows, "ok_count": len(rows) - errors, "error_count": errors}

    def probed_indices(self, prop: str) -> list[int]:
        """Every track index an op asked ``prop`` about, across all batches, in order."""
        out: list[int] = []
        for batch in self.batches:
            for op in batch:
                found = TRACK_PROP_PATH.match(str(op.get("path", "")))
                if found is not None and found.group(2) == prop:
                    out.append(int(found.group(1)))
        return out


class DeadClient(LiveClient):
    """A LiveClient whose socket is not there, which is what a closed Live looks like."""

    def send(
        self,
        handler: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        raise LiveConnectionError(
            "No connection to 127.0.0.1:9878 (ConnectionRefusedError: Connection refused)"
        )


def patience_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_playing: bool,
    playing: Sequence[int],
    fired: Sequence[int] | None = None,
) -> TransportState:
    """Read the transport once against a 15-track set answering the given rows."""
    client = RecordingClient(
        lambda ops: transport_answer(
            ops,
            tracks_row=tracks_row_for(len(playing)),
            is_playing=is_playing,
            playing=playing,
            fired=fired,
        )
    )
    monkeypatch.setattr(server, "_client_instance", lambda: client)
    state, missing = server._query_transport()
    assert missing is None
    assert state is not None
    return state


def test_query_transport_reads_the_whole_set_in_one_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_playing, the track count and both per-track slots come from one lom_batch.

    Both properties have to travel in the same request. Read in two round trips they
    could straddle the quantization boundary, and the pair would then describe two
    different moments while being reported as one transport state.
    """
    client = RecordingClient(
        lambda ops: transport_answer(
            ops,
            tracks_row=tracks_row_for(3),
            playing={0: 0, 1: -2, 2: -1},
            fired={0: -1, 1: -1, 2: -1},
        )
    )
    monkeypatch.setattr(server, "_client_instance", lambda: client)

    state, missing = server._query_transport()

    assert missing is None
    assert state is not None
    assert state.is_playing is True
    assert state.tracks_total == 3
    assert state.tracks_sounding == 1

    assert client.handlers == ["lom_batch"]
    assert len(client.batches) == 1
    assert len(client.batches[0]) <= server.MAX_BATCH_OPS
    assert client.probed_indices("playing_slot_index")[:3] == [0, 1, 2]
    assert client.probed_indices("fired_slot_index")[:3] == [0, 1, 2]


def test_settled_playback_reports_no_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clips playing and nothing pending is not a transition.

    The vector is the measured one: nine of the fifteen tracks hold no clip in the
    fired scene and read -2 for it, in a set that had been settled for six seconds.
    """
    state = patience_state(
        monkeypatch,
        is_playing=True,
        playing=PATIENCE_PLAYING,
        fired=[-1] * len(PATIENCE_PLAYING),
    )

    assert state.is_playing is True
    assert state.tracks_total == 15
    assert state.in_transition is False, "a settled set was reported as mid transition"
    assert state.tracks_sounding == sum(1 for slot in PATIENCE_PLAYING if slot >= 0)
    assert state.tracks_sounding == 6


def test_tracks_holding_no_clip_in_the_scene_are_not_a_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """-2 is a standing state, so a set full of it still reports no transition.

    Every track reads -2, which is what a set with no clip playing anywhere looks
    like: the quietest state the Session grid has, and not a transition signal.
    """
    state = patience_state(
        monkeypatch,
        is_playing=True,
        playing=[-2] * 15,
        fired=[-1] * 15,
    )

    assert state.in_transition is False
    assert state.tracks_sounding == 0


def test_a_pending_fire_is_the_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """A launch accepted and waiting for the bar line is what in_transition reports.

    playing_slot_index has not moved yet and carries the same -2 tracks as the settled
    reading, so nothing in it distinguishes this moment. fired_slot_index does.
    """
    state = patience_state(
        monkeypatch,
        is_playing=True,
        playing=PATIENCE_PENDING_PLAYING,
        fired=PATIENCE_PENDING_FIRED,
    )

    assert state.in_transition is True
    assert state.tracks_sounding == sum(1 for slot in PATIENCE_PENDING_PLAYING if slot >= 0)

    # The same playing vector with the fire resolved, which is the reading taken two
    # steps later: fired_slot_index has fallen back to -1 on every track.
    settled = patience_state(
        monkeypatch,
        is_playing=True,
        playing=PATIENCE_PENDING_PLAYING,
        fired=[-1] * len(PATIENCE_PENDING_PLAYING),
    )

    assert settled.in_transition is False
    assert settled.tracks_sounding == state.tracks_sounding


def test_a_stopped_transport_sounds_nothing_and_is_not_in_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a stop, neither field may claim playback.

    playing_slot_index survives song.stop_playing unchanged, so counting it without
    consulting is_playing reports tracks that have already fallen silent, and reading
    -2 as a pending change reports a transition in a set that is not playing at all.
    """
    state = patience_state(
        monkeypatch,
        is_playing=False,
        playing=PATIENCE_PLAYING,
        fired=[-1] * len(PATIENCE_PLAYING),
    )

    assert state.is_playing is False
    assert state.tracks_total == 15
    assert state.tracks_sounding == 0, "a stopped transport reported tracks sounding"
    assert state.in_transition is False, "a stopped transport reported a transition"

    # The raw rows still carry what they carried while the set was playing: it is the
    # derivation that has to notice the stop, not Live.
    assert any(slot >= 0 for slot in PATIENCE_PLAYING)
    assert any(slot == -2 for slot in PATIENCE_PLAYING)


def test_transport_field_descriptions_match_the_derivation() -> None:
    """The descriptions name the property each field is actually read from.

    in_transition is derived from fired_slot_index and not from
    playing_slot_index == -2, and tracks_sounding counts playing_slot_index only while
    is_playing.
    """
    fields = TransportState.model_fields

    transition = fields["in_transition"].description or ""
    assert "fired_slot_index" in transition
    assert "playing_slot_index == -2" not in transition
    # -2 may still be named, but only to say what it is not.
    assert "quantization" in transition

    sounding = fields["tracks_sounding"].description or ""
    assert "playing_slot_index" in sounding
    assert "is_playing" in sounding
    # It has to say what it does not count, because a caller compares it with what
    # they hear and the two are not the same number.
    assert "not" in sounding.casefold()


def test_query_transport_says_why_the_block_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No connection and a refused row both answer with a code and a sentence."""
    monkeypatch.setattr(server, "_client_instance", DeadClient)

    state, missing = server._query_transport()

    assert state is None
    assert missing is not None
    assert missing["code"] == "not_connected"
    assert missing["reason"].endswith(".")
    assert "connection" in missing["reason"].casefold()
    assert "not_connected" not in missing["reason"]

    refused_row = {
        "status": "error",
        "path": "song.tracks",
        "code": "bad_path",
        "message": "song.tracks could not be resolved",
    }
    client = RecordingClient(lambda ops: transport_answer(ops, tracks_row=refused_row))
    monkeypatch.setattr(server, "_client_instance", lambda: client)

    state, missing = server._query_transport()

    assert state is None
    assert missing is not None
    assert missing["code"] == "transport_unreadable"
    assert "song.tracks" in missing["reason"]
    assert "bad_path" in missing["reason"]
    assert missing["reason"].endswith(".")

    # The case the rule is really about: the set reads, and one per-track row inside it
    # does not. A state taken over three of four tracks is not a smaller measurement than
    # one taken over four, it is a wrong one, so no state may be shipped for it. It holds
    # for either property: a missing fired_slot_index loses the transition just as a
    # missing playing_slot_index loses the count.
    for index, prop in ((2, "playing_slot_index"), (3, "fired_slot_index")):
        partial = RecordingClient(
            lambda ops, index=index, prop=prop: transport_answer(
                ops,
                tracks_row=tracks_row_for(4),
                playing={0: 0, 1: -1, 2: -1, 3: 0},
                broken={(index, prop): "no_such_path"},
            )
        )
        monkeypatch.setattr(server, "_client_instance", lambda partial=partial: partial)

        state, missing = server._query_transport()

        assert state is None, f"a state missing {prop} for track {index} was shipped"
        assert missing is not None
        assert missing["code"] == "transport_unreadable"
        assert f"track {index}" in missing["reason"]
        assert prop in missing["reason"]
        assert "no_such_path" in missing["reason"]
        assert missing["reason"].endswith(".")

    # The same rule for a row that answered with something that is not a slot index.
    for index, prop in ((1, "playing_slot_index"), (0, "fired_slot_index")):
        mistyped = RecordingClient(
            lambda ops, index=index, prop=prop: transport_answer(
                ops,
                tracks_row=tracks_row_for(4),
                playing={0: 0, 1: -1, 2: -1, 3: 0},
                broken={(index, prop): "1"},
            )
        )
        monkeypatch.setattr(server, "_client_instance", lambda mistyped=mistyped: mistyped)

        state, missing = server._query_transport()

        assert state is None
        assert missing is not None
        assert missing["code"] == "transport_unreadable"
        assert f"track {index}" in missing["reason"]
        assert prop in missing["reason"]
        assert missing["reason"].endswith(".")


def test_query_transport_chunks_a_set_past_the_batch_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set larger than one probe is read in full, each track asked each property once.

    The count comes from the row's own ``count`` and not from its value list: the
    script stops encoding a collection at its list_cap, so the list is a ceiling.
    """
    total = server.MAX_BATCH_OPS + 500
    tracks_row = tracks_row_for(total, listed=128)
    client = RecordingClient(lambda ops: transport_answer(ops, tracks_row=tracks_row))
    monkeypatch.setattr(server, "_client_instance", lambda: client)

    state, missing = server._query_transport()

    assert missing is None
    assert state is not None
    assert state.tracks_total == total
    assert state.tracks_sounding == 0
    assert state.in_transition is False

    assert len(client.batches) >= 2
    for batch in client.batches:
        assert len(batch) <= server.MAX_BATCH_OPS
    # The set is larger than one batch may carry, so the ceiling is actually reached
    # rather than merely not exceeded: a helper that stopped chunking would send one
    # oversized request here instead of filling a batch to the cap.
    assert max(len(batch) for batch in client.batches) == server.MAX_BATCH_OPS
    for prop in ("playing_slot_index", "fired_slot_index"):
        probed = client.probed_indices(prop)
        assert len(set(probed)) == len(probed), f"{prop} was asked twice for some track"
        assert sorted(probed) == list(range(total))


@pytest.mark.anyio
async def test_analyze_audio_without_the_device_does_not_query_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was measured, so there is no moment to time-stamp and no read is spent."""
    measure_calls: list[int] = []
    queries: list[str] = []
    monkeypatch.setattr(server, "AnalyzerClient", silent_analyzer(measure_calls))
    monkeypatch.setattr(server, "_analyzer_target_name", lambda target: None)

    def counting_transport() -> tuple[TransportState | None, dict[str, Any] | None]:
        queries.append("called")
        return playing_transport(), None

    monkeypatch.setattr(server, "_query_transport", counting_transport)

    res = await server.analyze_audio(track="master")

    assert res["status"] == "device_not_found"
    assert res["transport"] is None
    assert res["transport_unavailable"]["code"] == "not_queried"
    assert "did not answer" in res["transport_unavailable"]["reason"]
    assert measure_calls == [400]
    assert queries == []


# ----------------------------------------------------------------- file identity


def test_file_source_block(tmp_path: Path) -> None:
    """The file source block reports path, size_bytes and an ISO formatted mtime."""
    f = tmp_path / "test.als"
    f.write_text("content 1")
    src1 = server._file_source(f)

    assert src1["path"] == str(f.resolve())
    assert src1["size_bytes"] == len("content 1")
    assert "T" in src1["mtime"] and src1["mtime"].endswith("Z")

    # Change file: size and mtime change
    time.sleep(0.01)
    f.write_text("longer modified content 2")
    src2 = server._file_source(f)

    assert src2["size_bytes"] == len("longer modified content 2")
    assert src1 != src2


def test_file_source_reports_a_missing_file_as_missing(tmp_path: Path) -> None:
    """A file that is not there answers with nulls and a reason, never with zeroes."""
    gone = tmp_path / "gone.als"

    src = server._file_source(gone)

    assert src["path"] == str(gone.resolve())
    assert src["size_bytes"] is None
    assert src["mtime"] is None
    assert src["code"] == "not_found"
    assert "gone.als" in src["reason"]
    assert src["reason"].endswith(".")


def test_als_read_carries_source(tmp_path: Path) -> None:
    """als_read carries the source block in its result."""
    f = tmp_path / "dummy.als"
    xml_content = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Ableton MajorVersion="5" MinorVersion="12.0_12049" SchemaChangeCount="3" '
        b'Creator="Ableton Live 12.0" Revision=""><LiveSet><Tracks/></LiveSet></Ableton>'
    )
    with gzip.open(f, "wb") as gz:
        gz.write(xml_content)

    res = server.als_read(str(f))
    assert res["ok"] is True
    assert "source" in res
    assert res["source"]["path"] == str(f.resolve())
    assert res["source"]["size_bytes"] == f.stat().st_size
    assert res["source"]["mtime"] is not None


def test_get_clip_audio_carries_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_clip on an audio clip carries the source block of its sample file."""
    audio_sample = tmp_path / "kick.wav"
    audio_sample.write_bytes(b"RIFF dummy wav data")

    def fake_run(spec_id: str, **kw: Any) -> dict[str, Any]:
        if spec_id == "clip_slot.has_clip":
            return {"ok": True, "value": True}
        if spec_id == "clip.file_path":
            return {"ok": True, "value": str(audio_sample)}
        return {"ok": True, "value": False}

    def fake_read_fields(specs: Any) -> tuple[dict[str, Any], None]:
        fields = {
            "is_midi_clip": {"ok": True, "value": False},
            "is_audio_clip": {"ok": True, "value": True},
        }
        return fields, None

    monkeypatch.setattr(server, "_run", fake_run)
    monkeypatch.setattr(server, "_read_fields", fake_read_fields)

    res = server.get_clip(track=0, slot=0)
    assert res["ok"] is True
    assert res["is_midi"] is False
    assert "source" in res
    assert res["source"]["path"] == str(audio_sample.resolve())
    assert res["source"]["size_bytes"] == len(b"RIFF dummy wav data")


# ``MasterTrack`` is Live 11's spelling and ``MainTrack`` Live 12's. Either one carries
# the tempo, and the expression below reaches the ``Tempo/Manual`` under both.
MINIMAL_ALS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Ableton MajorVersion="5" MinorVersion="12.0_12203" SchemaChangeCount="3" '
    'Creator="Ableton Live 12.4.5" Revision="fixture">'
    "<LiveSet><Tracks/>"
    "<MasterTrack><DeviceChain><Mixer>"
    '<Tempo><Manual Value="120" /></Tempo>'
    "</Mixer></DeviceChain></MasterTrack>"
    "</LiveSet></Ableton>"
)


def write_als(path: Path) -> Path:
    """Write the fixture as the gzip-compressed XML a .als actually is."""
    path.write_bytes(gzip.compress(MINIMAL_ALS.encode("utf-8")))
    return path


def test_als_write_preflight_carries_the_file_identity(tmp_path: Path) -> None:
    """The dry run says which file it resolved the expression against."""
    f = write_als(tmp_path / "Preflight.als")

    res = server.als_write(
        str(f),
        "attribute",
        confirm=False,
        expression=".//Tempo/Manual",
        value="126",
    )

    assert res["confirm_required"] is True
    assert res["source"]["path"] == str(f.resolve())
    assert res["source"]["size_bytes"] == f.stat().st_size
    assert res["source"]["mtime"] is not None
    assert res["selects"]["matches"] == 1
    assert res["selects"]["sample"][0]["current"] == "120"


def test_als_write_reports_the_file_before_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two source blocks around one write, each agreeing with the size the writer read.

    Live is made to answer with a different set's path, which is the real reason the
    write is allowed to go ahead: the check is about which file Live holds, not about
    whether a Live process exists.
    """
    f = write_als(tmp_path / "Edited.als")
    elsewhere = tmp_path / "Another.als"

    class OtherSetOpen:
        def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
            assert path == "song.file_path"
            return {"value": str(elsewhere)}

    monkeypatch.setattr(server, "_client_instance", OtherSetOpen)
    size_before = f.stat().st_size

    res = server.als_write(
        str(f),
        "attribute",
        confirm=True,
        expression=".//Tempo/Manual",
        value="126",
        attribute="Value",
    )

    assert res["ok"] is True
    size_after = f.stat().st_size
    assert res["source"]["path"] == str(f.resolve())
    assert res["source"]["path"] == res["source_after"]["path"]
    assert res["source"]["size_bytes"] == size_before
    assert res["source_after"]["size_bytes"] == size_after
    assert res["source_after"]["mtime"] is not None
    # The note the result carries claims exactly this, so it is asserted rather than read.
    assert res["source"]["size_bytes"] == res["size_before"]
    assert res["source_after"]["size_bytes"] == res["size_after"]


# ---------------------------------------------- a count nothing was read for
#: Which Session slots the stand-in set below holds a clip in.
FILLED_SLOTS = frozenset({(0, 0), (0, 2), (1, 1)})

_HAS_CLIP = re.compile(r"^song\.tracks\[(\d+)\]\.clip_slots\[(\d+)\]\.has_clip$")

_CLIP_FIELD = re.compile(r"^song\.tracks\[(\d+)\]\.clip_slots\[(\d+)\]\.clip\.(\w+)$")


class FakeSet:
    """A two track, four scene set holding three Session clips, answered over the wire.

    It exists for one question: whether a survey taken with ``clips=False`` reports a
    Session grid it never probed. The grid is deliberately not empty, so a result that
    says it is, is wrong rather than merely uninformative.
    """

    def __init__(self) -> None:
        self.paths: list[str] = []

    def script_info(self) -> dict[str, Any]:
        """Answer the handshake get_session takes before anything else."""
        return {"script": "live-maestro", "version": "test"}

    def describe(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Answer lom_describe for the song object, with its collection counts."""
        assert path == "song", f"the survey described {path!r} rather than the song"
        return {
            "path": "song",
            "properties": [
                {"name": "tempo", "value": 120.0},
                {"name": "is_playing", "value": False},
            ],
            "children": [
                {"name": "tracks", "count": 2},
                {"name": "return_tracks", "count": 0},
                {"name": "scenes", "count": 4},
            ],
        }

    def batch(
        self,
        ops: Sequence[Mapping[str, Any]],
        *,
        atomic: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Serve one batched read, recording every path it was asked for."""
        rows: list[dict[str, Any]] = []
        for op in ops:
            path = str(op.get("path"))
            self.paths.append(path)
            rows.append(self._serve(path))
        return {"results": rows}

    def _serve(self, path: str) -> dict[str, Any]:
        """Answer one path, in the row shape the Remote Script sends."""
        slot = _HAS_CLIP.match(path)
        if slot is not None:
            return {"path": path, "value": (int(slot.group(1)), int(slot.group(2))) in FILLED_SLOTS}
        field = _CLIP_FIELD.match(path)
        if field is not None:
            named = field.group(3)
            if named == "name":
                return {"path": path, "value": f"clip {field.group(1)}/{field.group(2)}"}
            if named == "length":
                return {"path": path, "value": 4.0}
            return {"path": path, "value": True}
        if path.endswith(".arrangement_clips[0].name"):
            return {"code": "bad_path", "message": f"{path} does not resolve"}
        if path.endswith(".name"):
            return {"path": path, "value": path}
        return {"path": path, "value": 0.0}


def test_a_survey_that_skipped_the_clip_sweep_reports_no_clip_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_session(clips=False) probes no slot, so it may not answer about the grid.

    The stand-in set holds three Session clips. With the sweep on they are counted;
    with it off the same set must come back with no count rather than with zero, which
    would be a report of an empty grid on a full one.
    """
    swept = FakeSet()
    monkeypatch.setattr(server, "_client_instance", lambda: swept)
    full = server.get_session(clips=True, devices=False)

    assert full["ok"] is True
    assert full["session_clip_count"] == 3
    assert [len(track["session_clips"]) for track in full["session"]["tracks"]] == [2, 1]
    assert server.SESSION_ONLY_SURVEY_NOTE in full["notes"]
    assert server.SESSION_CLIPS_NOT_PROBED_NOTE not in full["notes"]

    skipped = FakeSet()
    monkeypatch.setattr(server, "_client_instance", lambda: skipped)
    fast = server.get_session(clips=False, devices=False)

    assert fast["ok"] is True
    assert not any(".clip_slots[" in path for path in skipped.paths), (
        "clips=False probed a slot after all, so the count would be a measurement"
    )
    assert fast["session_clip_count"] is None
    assert [track["session_clips"] for track in fast["session"]["tracks"]] == [None, None]
    assert server.SESSION_CLIPS_NOT_PROBED_NOTE in fast["notes"]
    assert server.SESSION_ONLY_SURVEY_NOTE not in fast["notes"]
    assert "not probed" in server.SESSION_CLIPS_NOT_PROBED_NOTE


# ------------------------------------------- an empty chain and a missing track
#: A device probe on a track that exists stops inside the devices collection. On a
#: track that does not exist it stops one segment higher, at the track index itself,
#: which is what tells the two apart without a second read.
_DEVICE_PROBE = re.compile(r"^song\.(tracks|return_tracks)\[(\d+)\]\.devices\[(\d+)\]\.")


class BareSet:
    """A set with a fixed number of tracks, none of which holds a device.

    The error row it answers a probe with carries the path the Remote Script stopped
    resolving at (docs/protocol.md section 4), which is the only thing in the reply
    that distinguishes an empty chain from a track that is not there.
    """

    def __init__(self, track_count: int = 4, return_count: int = 2) -> None:
        self.track_count = track_count
        self.return_count = return_count

    def batch(
        self,
        ops: Sequence[Mapping[str, Any]],
        *,
        atomic: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Answer every probe, in the order it was asked."""
        rows = [self._serve(str(op.get("path"))) for op in ops]
        return {
            "results": rows,
            "ok_count": 0,
            "error_count": len(rows),
        }

    def _serve(self, path: str) -> dict[str, Any]:
        """Refuse a device probe, at the track index or inside the devices collection."""
        probe = _DEVICE_PROBE.match(path)
        assert probe is not None, f"the fake was asked for {path!r}, which is not a device probe"
        collection, index, device = probe.group(1), int(probe.group(2)), int(probe.group(3))
        length = self.track_count if collection == "tracks" else self.return_count
        if index >= length:
            return {
                "status": "error",
                "code": "index_out_of_range",
                "message": f"index {index} is beyond the {length} entries of song.{collection}",
                "path": f"song.{collection}[{index}]",
            }
        return {
            "status": "error",
            "code": "index_out_of_range",
            "message": f"index {device} is beyond the 0 entries of song.{collection}[{index}]"
            ".devices",
            "path": f"song.{collection}[{index}].devices[{device}]",
        }


def test_get_devices_tells_an_empty_chain_apart_from_a_track_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero count answers two different questions, and only one of them is a chain.

    A stale index is the case this protects: a caller checking track 99 of a four track
    set must be told there is no such track, not that the track is fine and bare.
    """
    monkeypatch.setattr(server, "_client_instance", BareSet)

    empty = server.get_devices(track=0)
    assert empty["ok"] is True
    assert empty["device_count"] == 0
    assert empty["devices"] == []
    assert empty["note"] == "No devices on this track."
    assert "code" not in empty

    missing = server.get_devices(track=99)
    assert missing["ok"] is False
    assert missing["code"] == "index_out_of_range"
    assert "99" in missing["error"]
    assert missing["error"] != missing["code"]
    assert "index_out_of_range" not in missing["error"]
    assert "message" not in missing

    missing_return = server.get_devices(track=9, kind="return")
    assert missing_return["ok"] is False
    assert missing_return["code"] == "index_out_of_range"
    assert "return" in missing_return["error"]

    bare_return = server.get_devices(track=1, kind="return")
    assert bare_return["ok"] is True
    assert bare_return["device_count"] == 0

    doc = server.get_devices.__doc__ or ""
    assert "an index with no track behind it is refused" in doc
