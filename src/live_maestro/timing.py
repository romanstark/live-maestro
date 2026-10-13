"""Typed musical-time requests; nested MCP schemas stay fully inlined."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from live_maestro.toolargs import inline_schema_refs


class TempoPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    beat: float = Field(ge=0, description="Quarter-note beat from song zero.")
    bpm: float = Field(ge=20, le=999, description="Tempo in beats per minute.")
    shape: Literal["step", "ramp"] = Field(
        default="step", description="Interpolation from this event toward the next one."
    )


class WarpPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    seconds: float = Field(ge=0, description="Seconds from the beginning of the source file.")
    beat: float = Field(ge=0, description="Clip-local quarter-note beat.")


TempoPointArg = Annotated[
    TempoPoint, WithJsonSchema(inline_schema_refs(TempoPoint.model_json_schema()))
]
WarpPointArg = Annotated[
    WarpPoint, WithJsonSchema(inline_schema_refs(WarpPoint.model_json_schema()))
]
