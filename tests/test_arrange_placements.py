"""The arrange list mode, driven against a Live stand-in that answers in wire shape.

A list of 50 placements onto different beats costs one round trip for the
duplication, and the result names the actual beat each clip landed on.

:class:`FakeLive` answers collections in Live's wire shape, ``{"__lom__", "path",
"name"}`` (docs/protocol.md section 5.3), and never the request spelling ``{"__path__"}``
of section 5.5, which is what an argument passed to a method looks like. A fake using
the request spelling confirms the bug it should catch, so the first test here guards the
shape.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from live_maestro import server
from live_maestro.client import LiveCommandError
from live_maestro.toolargs import PlacementIn

#: A track's arrangement clip collection, e.g. 'song.tracks[0].arrangement_clips'.
_LIST_PATH = re.compile(r"^song\.tracks\[(\d+)\]\.arrangement_clips$")

#: One clip's start or end beat on the Arrangement timeline.
_TIME_PATH = re.compile(
    r"^song\.tracks\[(\d+)\]\.arrangement_clips\[(\d+)\]\.(start_time|end_time)$"
)

#: One clip's name or length, which the single-placement mode reads for the clip it
#: identified as the one it placed.
_DETAIL_PATH = re.compile(
    r"^song\.tracks\[(\d+)\]\.arrangement_clips\[(\d+)\]\.(name|length)$"
)

#: The object a duplication call sits on.
_TRACK_PATH = re.compile(r"^song\.tracks\[(\d+)\]$")

#: What the Remote Script stops encoding a collection at (docs/protocol.md section 5.3).
LIST_CAP = 512

#: The typographic dash the house prose standard forbids, spelled by code point so that
#: this file does not itself carry the character it is checking for.
EM_DASH = chr(0x2014)


class FakeLive:
    """A stand-in for LiveClient that serves the three op shapes arrange sends.

    It keeps real state: every duplication inserts a clip into the destination track's
    list, kept sorted by start beat, so a before snapshot and an after snapshot read
    different worlds and the indices of later clips shift exactly as Live's do.

    Args:
        clips: Starting arrangement clips per track index, each (start_beat, end_beat).
            A track index absent from this mapping is one the fake does not know, and
            answers bad_path for.
        fallback_to_zero: True reproduces the documented duplication trap, in which a
            destination beat Live did not accept lands the clip at 0.0 while the call
            still reports success.
        swallow_calls: True reports success for every duplication and inserts nothing,
            which is the case a result may not call a placement.
        short_call_batch: True answers a batch of duplication calls with one row fewer
            than it was handed, which is the reply shape that shifts every later row
            onto the wrong op unless the gatherer pads for it.
        truncate_tracks: Track indices whose collection read comes back truncated at
            list_cap, with ``count`` still reporting the collection's true length.
        clip_length: How long a duplicated clip is, in beats.
    """

    def __init__(
        self,
        clips: Mapping[int, Sequence[tuple[float, float]]] | None = None,
        *,
        fallback_to_zero: bool = False,
        swallow_calls: bool = False,
        short_call_batch: bool = False,
        truncate_tracks: Sequence[int] = (),
        clip_length: float = 4.0,
    ) -> None:
        self.clips: dict[int, list[tuple[float, float]]] = {
            index: sorted(pairs, key=lambda pair: pair[0])
            for index, pairs in (clips or {}).items()
        }
        self.fallback_to_zero = fallback_to_zero
        self.swallow_calls = swallow_calls
        self.short_call_batch = short_call_batch
        self.truncate_tracks = tuple(truncate_tracks)
        self.clip_length = clip_length
        self.batches: list[list[dict[str, Any]]] = []
        self.calls: list[dict[str, Any]] = []

    def batch(
        self,
        ops: Sequence[Mapping[str, Any]],
        *,
        atomic: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Serve one lom_batch request and record the op list it carried."""
        sent = [dict(op) for op in ops]
        self.batches.append(sent)
        results = [self._serve(op) for op in sent]
        if self.short_call_batch and sent and all(op.get("op") == "call" for op in sent):
            results = results[:-1]
        errors = sum(1 for row in results if row.get("status") != "success")
        return {
            "results": results,
            "ok_count": len(results) - errors,
            "error_count": errors,
        }

    def call(
        self,
        path: str,
        method: str,
        args: Sequence[Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Serve one lom_call request, which is how the single-placement mode duplicates."""
        self.calls.append({"path": path, "method": method, "args": list(args or [])})
        if method != "duplicate_clip_to_arrangement":
            raise AssertionError(f"the fake was asked for an unexpected method {method!r}")
        called = _TRACK_PATH.match(path)
        if called is None:
            row = _bad_path(path)
        else:
            row = self._duplicate(int(called.group(1)), args, path)
        if row.get("status") != "success":
            raise LiveCommandError("lom_call", {"path": path, "method": method}, row)
        return {"path": path, "method": method, "result": None}

    # ------------------------------------------------------------------ serving
    def _serve(self, op: Mapping[str, Any]) -> dict[str, Any]:
        """Answer one operation, or say the path is not one this fake knows."""
        path = str(op.get("path") or "")
        if op.get("op") == "get":
            listed = _LIST_PATH.match(path)
            if listed is not None:
                return self._list_clips(int(listed.group(1)), path)
            timed = _TIME_PATH.match(path)
            if timed is not None:
                return self._read_time(
                    int(timed.group(1)), int(timed.group(2)), timed.group(3), path
                )
            detailed = _DETAIL_PATH.match(path)
            if detailed is not None:
                return self._read_detail(
                    int(detailed.group(1)), int(detailed.group(2)), detailed.group(3), path
                )
        if op.get("op") == "call" and op.get("method") == "duplicate_clip_to_arrangement":
            called = _TRACK_PATH.match(path)
            if called is not None:
                return self._duplicate(int(called.group(1)), op.get("args"), path)
        return _bad_path(path)

    def _list_clips(self, track: int, path: str) -> dict[str, Any]:
        """Answer a collection read with element handles, in Live's own reply shape."""
        if track not in self.clips:
            return _bad_path(path)
        clips = self.clips[track]
        handles = [
            {
                "__lom__": "Clip",
                "path": f"song.tracks[{track}].arrangement_clips[{index}]",
                "name": f"Clip {index + 1}",
            }
            for index in range(len(clips))
        ]
        row: dict[str, Any] = {
            "status": "success",
            "path": path,
            "type": "list",
            "class": "Vector",
            "count": len(clips),
            "value": handles,
        }
        if track in self.truncate_tracks:
            # count stays the collection's own length: only the encoding stops early.
            row["value"] = handles[:2]
            row["truncated"] = True
            row["list_cap"] = LIST_CAP
        return row

    def _read_time(self, track: int, index: int, field: str, path: str) -> dict[str, Any]:
        """Answer a start_time or end_time read against the clips as they stand now."""
        clips = self.clips.get(track)
        if clips is None or index >= len(clips):
            return _bad_path(path)
        start, end = clips[index]
        return {
            "status": "success",
            "path": path,
            "type": "float",
            "value": start if field == "start_time" else end,
        }

    def _read_detail(self, track: int, index: int, field: str, path: str) -> dict[str, Any]:
        """Answer a name or length read, so a clip can be named by the beat it sits on."""
        clips = self.clips.get(track)
        if clips is None or index >= len(clips):
            return _bad_path(path)
        start, end = clips[index]
        if field == "length":
            return {"status": "success", "path": path, "type": "float", "value": end - start}
        return {"status": "success", "path": path, "type": "str", "value": f"clip@{start}"}

    def _duplicate(self, track: int, args: Any, path: str) -> dict[str, Any]:
        """Insert a clip on the destination track, keeping the collection sorted."""
        if track not in self.clips:
            return _bad_path(path)
        assert isinstance(args, Sequence) and len(args) == 2, (
            f"duplicate_clip_to_arrangement takes a source clip and a beat, got {args!r}"
        )
        source = args[0]
        assert isinstance(source, Mapping) and "__path__" in source, (
            "the source clip travels as an object reference {'__path__': ...}, which is "
            f"the request spelling of a handle (docs/protocol.md section 5.5), got {source!r}"
        )
        if self.swallow_calls:
            return {"status": "success", "path": path, "value": None}
        start = 0.0 if self.fallback_to_zero else float(args[1])
        self.clips[track].append((start, start + self.clip_length))
        self.clips[track].sort(key=lambda pair: pair[0])
        return {"status": "success", "path": path, "value": None}


def _bad_path(path: str) -> dict[str, Any]:
    """Return the error row the Remote Script answers an unresolvable path with."""
    return {
        "status": "error",
        "code": "bad_path",
        "message": f"{path} does not resolve to an object in this set",
        "path": path,
    }


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeLive) -> FakeLive:
    """Put the fake in front of every socket call the server would make."""
    monkeypatch.setattr(server, "_client_instance", lambda: fake)
    return fake


# ----------------------------------------------------------------- the fixture
def test_the_fake_serves_the_wire_shape_live_sends() -> None:
    """A handle in a reply is keyed 'path'. '__path__' never comes back from Live."""
    fake = FakeLive({0: [(0.0, 4.0), (16.0, 20.0)]})
    reply = fake.batch([{"op": "get", "path": "song.tracks[0].arrangement_clips"}])

    row = reply["results"][0]
    assert row["status"] == "success"
    assert row["count"] == 2
    for handle in row["value"]:
        assert set(handle) == {"__lom__", "path", "name"}
        assert handle["path"].startswith("song.tracks[0].arrangement_clips[")
    assert "__path__" not in json.dumps(reply)


# ------------------------------------------------------------------ C2 criterion
def test_fifty_placements_name_the_actual_beat_of_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: 50 placements, one duplication round trip, the real beat named for each."""
    fake = _install(monkeypatch, FakeLive({0: []}))
    placements = [
        PlacementIn(track=0, slot=0, at_beat=float(i * 4)) for i in range(50)
    ]

    res = server.arrange(placements=placements)

    assert res["ok"] is True
    assert res["verified"] is True
    assert res["total"] == 50
    assert res["succeeded"] == 50
    assert res["failed"] == 0
    for i, row in enumerate(res["placements"]):
        assert row["ok"] is True, row
        assert row["requested_beat"] == float(i * 4)
        assert row["actual_beat"] == float(i * 4)
        assert row["end_beat"] == float(i * 4 + 4)
        assert "code" not in row
    # 50 == ARRANGE_CALL_CHUNK, so the duplication is the single round trip C2 asks for.
    assert server.ARRANGE_CALL_CHUNK == 50
    assert res["round_trips_by_phase"]["duplicate"] == 1
    assert res["round_trips"] == 4
    assert res["destinations"] == [
        {
            "track": 0,
            "arrangement_clips_before": 0,
            "arrangement_clips_after": 50,
            "clips_added": 50,
            "placements_requested": 50,
        }
    ]
    assert fake.clips[0] == [(float(i * 4), float(i * 4 + 4)) for i in range(50)]


def test_a_clip_already_on_the_beat_does_not_stand_in_for_a_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification is a difference, not a search of the whole track."""
    fake = _install(
        monkeypatch, FakeLive({0: [(16.0, 20.0)]}, fallback_to_zero=True)
    )

    res = server.arrange(placements=[{"track": 0, "slot": 0, "at_beat": 16.0}])

    # The measurement succeeded; the placement did not.
    assert res["ok"] is False
    assert res["verified"] is True
    row = res["placements"][0]
    assert row["ok"] is False
    assert row["requested_beat"] == 16.0
    assert row["code"] == "stack_fallback_to_zero"
    # actual_beat is a read-back, not a verdict: the clip really is at 0.0, and the
    # row says so rather than dropping a reading that was taken.
    assert row["actual_beat"] == 0.0
    assert row["end_beat"] == 4.0
    assert "0.0" in row["error"]
    assert "Delete" in row["error"]
    assert res["destinations"][0]["clips_added"] == 1
    # The clip that was already on beat 16 is still there and was not counted.
    assert fake.clips[0] == [(0.0, 4.0), (16.0, 20.0)]


def test_a_fallback_to_zero_is_named_as_the_documented_trap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every placement that collapsed onto 0.0 says so by code, not by silence."""
    _install(monkeypatch, FakeLive({0: []}, fallback_to_zero=True))
    placements = [
        {"track": 0, "slot": 0, "at_beat": 16.0},
        {"track": 0, "slot": 0, "at_beat": 32.0},
    ]

    res = server.arrange(placements=placements)

    assert res["ok"] is False
    assert res["verified"] is True
    assert res["succeeded"] == 0
    assert res["failed"] == 2
    for row in res["placements"]:
        assert row["ok"] is False
        assert row["code"] == "stack_fallback_to_zero"
    assert res["destinations"][0]["clips_added"] == 2


def test_a_clip_inserted_between_existing_clips_still_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new clip renumbers the ones after it, and the difference still holds."""
    _install(monkeypatch, FakeLive({0: [(0.0, 4.0), (32.0, 36.0)]}))

    res = server.arrange(placements=[{"track": 0, "slot": 0, "at_beat": 16.0}])

    assert res["ok"] is True
    assert res["verified"] is True
    row = res["placements"][0]
    assert row["actual_beat"] == 16.0
    assert row["end_beat"] == 20.0
    destination = res["destinations"][0]
    assert destination["arrangement_clips_before"] == 2
    assert destination["arrangement_clips_after"] == 3
    assert destination["clips_added"] == 1


def test_placements_chunk_at_the_batch_limits_and_report_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long list is split rather than refused, and every phase says what it cost."""
    monkeypatch.setattr(server, "MAX_BATCH_OPS", 4)
    monkeypatch.setattr(server, "ARRANGE_CALL_CHUNK", 3)
    fake = _install(monkeypatch, FakeLive({0: []}))
    placements = [{"track": 0, "slot": 0, "at_beat": float(i * 4)} for i in range(7)]

    res = server.arrange(placements=placements)

    assert res["ok"] is True
    for sent in fake.batches:
        assert len(sent) <= 4, sent
        if sent[0].get("op") == "call":
            assert len(sent) <= 3, sent
    assert res["round_trips_by_phase"] == {
        "snapshot_before": 1,
        "duplicate": 3,
        "snapshot_after": 5,
    }
    assert res["round_trips"] == 9
    assert len(fake.batches) == 9


def test_a_truncated_arrangement_read_is_reported_as_not_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial clip list cannot be differenced, so nothing is claimed about it."""
    _install(
        monkeypatch,
        FakeLive({0: [(0.0, 4.0), (8.0, 12.0), (24.0, 28.0)]}, truncate_tracks=(0,)),
    )

    res = server.arrange(placements=[{"track": 0, "slot": 0, "at_beat": 16.0}])

    assert res["verified"] is False
    assert res["code"] == "verification_incomplete"
    row = res["placements"][0]
    assert row["code"] == "not_measured"
    assert row["actual_beat"] is None
    assert "list_cap" in row["error"]
    destination = res["destinations"][0]
    assert destination["clips_added"] is None
    assert "list_cap" in destination["not_measured"]
    # Live's own count is honest under truncation: only the encoding stopped early.
    assert destination["arrangement_clips_before"] == 3
    assert destination["arrangement_clips_after"] == 4


def test_a_failed_duplication_call_reports_the_scripts_code_and_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script's own code survives the verification pass rather than being replaced."""
    _install(monkeypatch, FakeLive({0: []}))

    res = server.arrange(
        placements=[{"track": 0, "slot": 0, "at_beat": 16.0, "to_track": 9}]
    )

    assert res["ok"] is False
    row = res["placements"][0]
    assert row["destination_track"] == 9
    assert row["code"] == "bad_path"
    assert row["code"] != "not_measured"
    assert "song.tracks[9]" in row["error"]
    assert row["actual_beat"] is None


def test_both_modes_at_once_are_refused_and_nothing_is_sent_to_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-placement argument beside a list is a mistake, not a dropped argument.

    to_track is the case that matters: the list branch reads each placement's own
    destination, so a top-level to_track puts real clips on a track nobody named.
    """
    fake = _install(monkeypatch, FakeLive({0: [], 1: [], 5: []}))

    res = server.arrange(
        track=0, slot=0, at_beat=64.0, placements=[{"track": 1, "slot": 0, "at_beat": 8.0}]
    )

    assert res["ok"] is False
    assert res["code"] == "conflicting_arguments"
    assert res["error"] != res["code"]
    assert "message" not in res
    assert res["ignored"] == {"track": 0, "slot": 0, "at_beat": 64.0}
    assert fake.clips == {0: [], 1: [], 5: []}
    assert fake.batches == []
    assert fake.calls == []

    misrouted = server.arrange(to_track=5, placements=[{"track": 0, "slot": 0, "at_beat": 8.0}])

    assert misrouted["ok"] is False
    assert misrouted["code"] == "conflicting_arguments"
    assert misrouted["ignored"] == {"to_track": 5}
    assert "to_track" in misrouted["error"]
    assert fake.clips == {0: [], 1: [], 5: []}

    field = server.arrange.__annotations__
    assert "must be omitted" in str(field)


# ------------------------------------------------- the single-placement mode
def test_a_single_placement_names_the_clip_it_placed_not_the_last_on_the_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """arrangement_clips is ordered by start time, so the last entry is not the new one.

    Track 0 already holds clips at beats 100 and 200. A placement at beat 64 sorts in
    front of both, so anything that reads back the last entry answers about a clip the
    call never touched, with ok and verified true.
    """
    fake = _install(monkeypatch, FakeLive({0: [(100.0, 104.0), (200.0, 204.0)]}))

    res = server.arrange(track=0, slot=0, at_beat=64.0)

    assert res["ok"] is True
    assert res["verified"] is True
    assert res["at_beat"] == 64.0
    assert res["actual_beat"] == 64.0
    assert res["end_beat"] == 68.0
    assert res["arrangement_clip_count_before"] == 2
    assert res["arrangement_clip_count_after"] == 3
    assert res["placed_clip"]["start_time"]["value"] == 64.0
    assert res["placed_clip"]["end_time"]["value"] == 68.0
    assert res["placed_clip"]["name"]["value"] == "clip@64.0"
    assert "code" not in res
    assert fake.clips[0] == [(64.0, 68.0), (100.0, 104.0), (200.0, 204.0)]


def test_a_single_placement_that_fell_back_to_zero_is_not_reported_as_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented trap: Live refuses the beat, lands at 0.0, and still says success."""
    _install(
        monkeypatch,
        FakeLive({0: [(100.0, 104.0), (200.0, 204.0)]}, fallback_to_zero=True),
    )

    res = server.arrange(track=0, slot=0, at_beat=64.0)

    assert res["ok"] is False
    assert res["code"] == "stack_fallback_to_zero"
    assert res["at_beat"] == 64.0
    assert res["actual_beat"] == 0.0
    assert "0.0" in res["error"]
    assert res["error"] != res["code"]
    assert "message" not in res
    assert res["placed_clip"]["start_time"]["value"] == 0.0


def test_a_single_placement_nothing_answered_for_is_not_called_placed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call that reports success while no clip appeared is 'not_observed', not ok."""
    fake = _install(monkeypatch, FakeLive({0: [(100.0, 104.0)]}))
    fake.swallow_calls = True

    res = server.arrange(track=0, slot=0, at_beat=64.0)

    assert res["ok"] is False
    assert res["code"] == "not_observed"
    assert res["actual_beat"] is None
    assert res["end_beat"] is None
    assert res["placed_clip"] is None
    assert res["verified"] is False
    assert res["arrangement_clip_count_before"] == 1
    assert res["arrangement_clip_count_after"] == 1


def test_a_single_placement_to_another_track_verifies_on_that_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """to_track moves the destination, and the difference is taken there, not on track."""
    fake = _install(monkeypatch, FakeLive({0: [], 1: [(8.0, 12.0)]}))

    res = server.arrange(track=0, slot=0, at_beat=4.0, to_track=1)

    assert res["ok"] is True
    assert res["destination_track"] == 1
    assert res["actual_beat"] == 4.0
    assert res["arrangement_clip_count_before"] == 1
    assert res["arrangement_clip_count_after"] == 2
    assert fake.clips[0] == []


# ------------------------------------------------ the branches that say "unknown"
def test_two_clips_a_millibeat_apart_each_verify_against_their_own_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching is at millibeat resolution: a triplet stays distinct from its neighbour.

    The two beats sit one millibeat apart and the fake's collection reverses the order
    they were requested in, so a tolerance coarser than three decimals confirms each
    placement against the other one's clip while still reporting ok.
    """
    _install(monkeypatch, FakeLive({0: []}))

    res = server.arrange(
        placements=[
            {"track": 0, "slot": 0, "at_beat": 16.334},
            {"track": 0, "slot": 0, "at_beat": 16.333},
        ]
    )

    assert res["ok"] is True
    for row in res["placements"]:
        assert row["actual_beat"] == row["requested_beat"], row
        assert row["end_beat"] == row["requested_beat"] + 4.0, row
    assert server.BEAT_MATCH_DECIMALS == 3


def test_a_placement_no_clip_appeared_for_is_reported_as_not_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplication reported success and the track did not change: that is unknown.

    Neither the requested beat nor 0.0 gained a clip, so there is nothing to call the
    placement and the row says so rather than borrowing a clip that was already there.
    """
    fake = _install(monkeypatch, FakeLive({0: [(16.0, 20.0)]}))
    fake.swallow_calls = True

    res = server.arrange(placements=[{"track": 0, "slot": 0, "at_beat": 16.0}])

    assert res["ok"] is False
    assert res["succeeded"] == 0
    row = res["placements"][0]
    assert row["code"] == "not_observed"
    assert row["actual_beat"] is None
    assert row["end_beat"] is None
    assert row["ok"] is False
    assert "16.0" in row["error"]
    assert res["destinations"][0]["clips_added"] == 0


def test_a_reply_one_row_short_does_not_shift_later_placements_onto_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk that answered short is padded, so row N stays row N's own call result.

    Without the padding the second placement would read the third placement's call
    result, and the last placement would read nothing at all: every verdict after the
    missing row would belong to a different clip.
    """
    _install(monkeypatch, FakeLive({0: []}, short_call_batch=True))

    res = server.arrange(
        placements=[
            {"track": 0, "slot": 0, "at_beat": 0.0},
            {"track": 0, "slot": 0, "at_beat": 8.0},
            {"track": 0, "slot": 0, "at_beat": 16.0},
        ]
    )

    assert res["ok"] is False
    assert [row["requested_beat"] for row in res["placements"]] == [0.0, 8.0, 16.0]
    # The two calls that were answered landed and are verified against their own beats.
    for row in res["placements"][:2]:
        assert row["ok"] is True, row
        assert row["actual_beat"] == row["requested_beat"], row
    last = res["placements"][2]
    assert last["ok"] is False
    assert last["code"] == "missing_result"
    assert last["actual_beat"] is None


def test_missing_arguments_puts_the_machine_code_in_code() -> None:
    """A refusal names the machine code in 'code' and the reason in 'error'."""
    missing = server.arrange()
    assert missing["ok"] is False
    assert missing["code"] == "missing_arguments"
    assert missing["error"].endswith(".")
    assert missing["error"] != missing["code"]
    assert "message" not in missing

    empty = server.arrange(placements=[])
    assert empty["ok"] is False
    assert empty["code"] == "empty_placements"
    assert empty["error"] != empty["code"]
    assert "message" not in empty


def test_both_modes_carry_the_retry_warning_and_the_docstring_restores_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither mode may be retried blind, and the docstring still says how to build one."""
    _install(monkeypatch, FakeLive({0: []}))
    listed = server.arrange(placements=[{"track": 0, "slot": 0, "at_beat": 16.0}])
    assert server.ARRANGE_RETRY_NOTE in listed["notes"]

    single = server.arrange(track=0, slot=1, at_beat=64.0)
    assert server.ARRANGE_RETRY_NOTE in single["notes"]

    doc = server.arrange.__doc__ or ""
    assert "Repeating the same call adds another copy at the" in doc
    assert "needs the clip count in this answer checked first" in doc
    assert "Use create_clip and write_clip_notes to build the source clip" in doc
    assert "set_arrangement_time then play to hear where it landed" in doc
    assert EM_DASH not in doc
