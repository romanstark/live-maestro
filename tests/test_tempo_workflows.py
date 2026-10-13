"""Tempo copies preserve source data and integrate actual step/ramp segments."""

from __future__ import annotations

import gzip
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from live_maestro.als import tempo


def saved_set(path: Path) -> Path:
    path.write_bytes(
        gzip.compress(b"""<Ableton><LiveSet><MainTrack>
      <DeviceChain><Mixer><Tempo><Manual Value="120"/>
      <AutomationTarget Id="471"/></Tempo></Mixer></DeviceChain>
      <AutomationEnvelopes><Envelopes>
      <AutomationEnvelope Id="4"><EnvelopeTarget><PointeeId Value="999"/></EnvelopeTarget>
      <Automation><Events><FloatEvent Id="0" Time="0" Value="0.75"/></Events></Automation>
      </AutomationEnvelope>
      <AutomationEnvelope Id="9"><EnvelopeTarget><PointeeId Value="471"/></EnvelopeTarget>
      <Automation><Events><FloatEvent Id="0" Time="-63072000" Value="120"/></Events>
      </Automation></AutomationEnvelope></Envelopes></AutomationEnvelopes>
      </MainTrack></LiveSet></Ableton>""")
    )
    return path


POINTS = [
    {"beat": 0, "bpm": 120, "shape": "ramp"},
    {"beat": 8, "bpm": 180, "shape": "step"},
    {"beat": 16, "bpm": 90, "shape": "ramp"},
    {"beat": 32, "bpm": 60, "shape": "step"},
]


@pytest.mark.parametrize("beat", [0, 0.01, 4, 8, 12, 16, 24, 32, 10000])
def test_tempo_inverse_across_rising_falling_and_step_segments(beat: float) -> None:
    seconds = tempo.seconds_at(beat, POINTS)
    assert tempo.beat_at(seconds, POINTS) == pytest.approx(beat, abs=1e-10)
    assert tempo.seconds_at(8, POINTS) == pytest.approx(8 * math.log(1.5))
    assert tempo.seconds_at(16, POINTS) - tempo.seconds_at(8, POINTS) == pytest.approx(8 / 3)


def test_copy_resolves_target_preserves_source_and_other_envelopes(tmp_path: Path) -> None:
    source = saved_set(tmp_path / "source.als")
    before = source.read_bytes()
    target = tmp_path / "copy.als"
    assert tempo.write_copy(source, target, POINTS, False)["blocked"]
    assert not target.exists()
    result = tempo.write_copy(source, target, POINTS, True)
    assert result["xml_verified"] and not result["live_verified"]
    assert tempo.read(target)["events"] == POINTS
    assert source.read_bytes() == before
    root = ET.fromstring(gzip.decompress(target.read_bytes()))
    unrelated = root.find('.//AutomationEnvelope[@Id="4"]//FloatEvent')
    assert unrelated is not None and unrelated.get("Value") == "0.75"
    with pytest.raises(ValueError, match="new .als copy"):
        tempo.write_copy(source, target, POINTS, True)


@pytest.mark.parametrize(
    "points",
    [
        [],
        [{"beat": 1, "bpm": 120}],
        [{"beat": 0, "bpm": float("nan")}],
        [{"beat": 0, "bpm": 10}],
        [{"beat": 0, "bpm": 120}, {"beat": 0, "bpm": 150}],
    ],
)
def test_invalid_maps_never_publish(tmp_path: Path, points: list[dict]) -> None:
    source = saved_set(tmp_path / "source.als")
    target = tmp_path / "copy.als"
    with pytest.raises(ValueError):
        tempo.write_copy(source, target, points, True)
    assert not target.exists()


def test_curved_tempo_is_refused_instead_of_treated_as_linear(tmp_path: Path) -> None:
    source = saved_set(tmp_path / "curved.als")
    xml = gzip.decompress(source.read_bytes()).replace(
        b'Time="-63072000"', b'Time="0" CurveControl1X="0.4"'
    )
    source.write_bytes(gzip.compress(xml))
    with pytest.raises(ValueError, match="curve-aware"):
        tempo.read(source)
