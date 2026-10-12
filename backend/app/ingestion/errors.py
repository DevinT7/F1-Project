"""Errors raised by the ingestion layer. FastF1 exceptions never escape past these."""
from __future__ import annotations


class IngestionError(RuntimeError):
    """Base class for ingestion and storage errors."""


class SessionNotFoundError(IngestionError):
    """The session does not exist upstream, or is not stored locally."""


class TelemetryUnavailableError(IngestionError):
    """No usable telemetry exists for the requested lap."""


class LapInvalidError(IngestionError):
    """No lap satisfies the requested validity constraint."""


class DriverNotFoundError(SessionNotFoundError):
    """The driver has no laps in this session."""


class LapNotFoundError(SessionNotFoundError):
    """The requested lap number does not exist for the driver."""


class PayloadTooLargeError(IngestionError):
    """The requested response would exceed the transport size cap."""
