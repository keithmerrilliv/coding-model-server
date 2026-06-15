"""Access-log routing for high-frequency dashboard poll endpoints.

The dashboard polls /health and several /v1/admin + /v1/autonomous endpoints at
~1 Hz. Those access lines drown real signal in the main journal, so this filter
reroutes successful poll-GET lines to a rotating side-channel file (or drops
them) while letting everything else through. Extracted from server.py to keep
the app module focused on wiring.
"""
import logging
import logging.handlers
import os
from typing import Optional

# Paths the dashboard polls at ~1 Hz. Settable side-channel via
# CODING_MODEL_POLL_ACCESS_LOG; empty disables the file and falls back to silent drop.
_POLL_ACCESS_EXACT = frozenset({
    "/health",
    "/v1/admin/metrics",
    "/v1/admin/gpu_stats",
    "/v1/admin/active_model",
})
# Prefixes for parameterized poll endpoints — the dashboard's SpecDetail
# page polls /v1/autonomous/specs/{spec_id} and /v1/autonomous/gates?spec_id=…
# every ~15s, which exact-match can't catch. We match `path == prefix` OR
# `path.startswith(prefix + "/")` so a future `/v1/autonomous/specsfoo`
# (404 anyway) doesn't accidentally get silenced.
_POLL_ACCESS_PREFIXES = (
    "/v1/autonomous/specs",
    "/v1/autonomous/gates",
)


def _is_poll_access_path(path: str) -> bool:
    if path in _POLL_ACCESS_EXACT:
        return True
    return any(
        path == p or path.startswith(p + "/")
        for p in _POLL_ACCESS_PREFIXES
    )


_POLL_ACCESS_LOG_PATH = os.getenv(
    "CODING_MODEL_POLL_ACCESS_LOG", "/tmp/coding-model-poll-access.log"
)


def build_poll_access_logger() -> Optional[logging.Logger]:
    """Side-channel logger for noisy 1 Hz dashboard polls.

    Rotating file handler (1 MB × 3 backups) so the file never grows without
    bound. Returns None when the path is empty (drop without side-channel)
    or when the handler can't be created (e.g. unwritable directory) — the
    filter falls back to silent drop in that case.
    """
    if not _POLL_ACCESS_LOG_PATH:
        return None
    try:
        handler = logging.handlers.RotatingFileHandler(
            _POLL_ACCESS_LOG_PATH, maxBytes=1_000_000, backupCount=3,
        )
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Could not open poll access log %s: %s — dropping silently",
            _POLL_ACCESS_LOG_PATH, exc,
        )
        return None
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    log = logging.getLogger("qwen.poll_access")
    log.setLevel(logging.INFO)
    # propagate=False keeps these out of the root handler chain — the whole
    # point is to *isolate* poll lines from the main journal.
    log.propagate = False
    # Replace any prior handler (lifespan can re-run on reload).
    for h in list(log.handlers):
        log.removeHandler(h)
    log.addHandler(handler)
    return log


class RoutePollAccessLines(logging.Filter):
    """Reroute uvicorn access lines for high-frequency dashboard polls.

    When the request matches a poll path and is a successful GET, the access
    line is forwarded to the side-channel logger (if available) and dropped
    from the main uvicorn.access pipeline. All other lines (errors, non-poll
    endpoints) pass through unchanged.

    Attached inside the FastAPI lifespan startup hook (not at module load),
    because `uvicorn.run` calls `logging.config.dictConfig` AFTER importing
    the app module — and dictConfig explicitly clears any preexisting
    filters on loggers it reconfigures, which includes `uvicorn.access`.
    """

    def __init__(self, side_channel: Optional[logging.Logger]):
        super().__init__()
        self.side_channel = side_channel

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access record.args is (client_addr, method, full_path,
        # http_version, status_code). full_path INCLUDES the query string
        # (e.g. "/v1/admin/metrics?window_seconds=60"), so we must strip it
        # before exact-matching — otherwise only /health (which the dashboard
        # calls with no query) gets caught and the noisier endpoints leak.
        # Match on args directly; string-search against getMessage() is
        # brittle since the format ends in `%(status_code)s` and AccessFormatter
        # rendering ("200 OK") happens later in the pipeline.
        args = record.args
        if not args or len(args) < 5:
            return True
        method, path, _http_ver, status = args[1], args[2], args[3], args[4]
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            return True
        path_only = path.split("?", 1)[0] if isinstance(path, str) else path
        if method != "GET" or status_int != 200 or not isinstance(path_only, str):
            return True
        if not _is_poll_access_path(path_only):
            return True
        # Matched a poll endpoint. Forward to side-channel if configured;
        # always drop from the main pipeline.
        if self.side_channel is not None:
            try:
                self.side_channel.info(record.getMessage())
            except Exception:
                # Never let a failing side-channel break access logging.
                pass
        return False
