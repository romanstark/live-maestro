"""Public input descriptions for isolated drum analysis."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from live_maestro.toolargs import inline_schema_refs


class DrumStem(BaseModel):
    """The caller identifies a separated instrument; no kit classifier is implied."""

    model_config = ConfigDict(extra="forbid")
    instrument: Literal["kick", "snare"] = Field(
        description="Known isolated source: kick or snare."
    )
    path: str = Field(description="Local WAV/FLAC stem path; finite mono/stereo audio.")
    source_offset_seconds: float = Field(
        default=0, ge=0, description="Position of the file's first sample in the original source."
    )
    source_sample_rate: int | None = Field(
        default=None,
        ge=8000,
        le=192000,
        description="Original source Hz from UVR timeline; None uses this stem's file rate.",
    )
    calibration_ms: float = Field(
        default=0,
        ge=-100,
        le=100,
        description="Known detector lateness in ms, subtracted from detected attacks.",
    )
    midi_pitch: int | None = Field(
        default=None, ge=0, le=127, description="Override GM kick 36 or snare 38 for your kit."
    )
    reference_path: str | None = Field(
        default=None,
        description="Optional same-rate/channel isolated reference for contamination residual.",
    )


class BeatAnchor(BaseModel):
    """One reviewed source-second/quarter-note position."""

    model_config = ConfigDict(extra="forbid")
    seconds: float = Field(ge=0, description="Absolute source seconds.")
    beat: float = Field(ge=0, description="Quarter-note position in the original source.")


DrumStemArg = Annotated[DrumStem, WithJsonSchema(inline_schema_refs(DrumStem.model_json_schema()))]
BeatAnchorArg = Annotated[
    BeatAnchor, WithJsonSchema(inline_schema_refs(BeatAnchor.model_json_schema()))
]
