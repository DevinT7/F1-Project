"""Errors raised by the lap alignment / delta engine."""
from __future__ import annotations

from ..ingestion.errors import IngestionError, LapInvalidError

__all__ = ["InsufficientDataError", "LapInvalidError"]


class InsufficientDataError(IngestionError):
    """Alignment ran but the result fails an integrity check; never return its numbers."""
