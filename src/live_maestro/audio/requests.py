"""Public paths and ranges for Live capture and A/B comparison."""

from typing import Annotated

from pydantic import Field

AudioFileArg = Annotated[str, Field(description="Local mono/stereo audio file path (WAV/FLAC).")]
AudioOutputArg = Annotated[str, Field(description="Parent directory for a unique audio run.")]
SourceStartArg = Annotated[float, Field(ge=0, description="Start in absolute source-file seconds.")]
SourceEndArg = Annotated[float, Field(gt=0, description="Exclusive end in source-file seconds.")]
