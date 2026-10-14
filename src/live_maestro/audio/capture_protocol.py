"""Capture control shares the Analyzer socket; audio stays in local files."""

CAPTURE_SCHEMA = 1
OPEN_ADDRESS = "/maestro/capture/open"
START_ADDRESS = "/maestro/capture/start"
STOP_ADDRESS = "/maestro/capture/stop"
REPORT_ADDRESS = "/maestro/capture/report"
PATH_ADDRESS = "/maestro/capture/path"
REPORT_FIELDS: tuple[str, ...] = ("schema", "request_id", "phase", "sample_rate", "track_id",
                                 "counter", "song_beat", "host_flags")
