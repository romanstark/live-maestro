"""Public paths and ranges for Live capture and A/B comparison."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from live_maestro.toolargs import inline_schema_refs

AudioFileArg = Annotated[str, Field(description="Local mono/stereo audio file path (WAV/FLAC).")]
AudioOutputArg = Annotated[str, Field(description="Parent directory for a unique audio run.")]
SourceStartArg = Annotated[float, Field(ge=0, description="Start in absolute source-file seconds.")]
SourceEndArg = Annotated[float, Field(gt=0, description="Exclusive end in source-file seconds.")]


class BeatAnchor(BaseModel):
    """One point of a source's tempo map."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    seconds: float = Field(ge=0, description="Seconds in that audio file.")
    beat: float = Field(ge=0, description="Song quarter beat sounding at those seconds.")


BeatAnchorArg = Annotated[
    BeatAnchor, WithJsonSchema(inline_schema_refs(BeatAnchor.model_json_schema()))
]
