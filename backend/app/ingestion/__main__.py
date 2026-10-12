"""Ingestion CLI. Run from backend/:

    python -m app.ingestion ingest --year 2024 --round 5 --session Q [--force]
    python -m app.ingestion season --year 2024 --sessions Q,R [--force]
    python -m app.ingestion events --year 2024 [--refresh]
    python -m app.ingestion list [--year 2024]
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

import structlog

from ..config import settings
from ..storage.repository import ParquetRepository
from .errors import IngestionError
from .fastf1_client import FastF1Client
from .service import IngestionService


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion", description="RaceIQ FastF1 ingestion")
    parser.add_argument("--data-dir", default=settings.data_dir, help="normalized session store")
    parser.add_argument("--cache-dir", default=settings.fastf1_cache_dir, help="FastF1 HTTP cache")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ingest one session")
    ingest.add_argument("--year", type=int, required=True)
    ingest.add_argument("--round", type=int, required=True)
    ingest.add_argument("--session", required=True, help="FP1 FP2 FP3 SQ SS S Q R")
    ingest.add_argument("--force", action="store_true", help="re-ingest even if stored")

    season = sub.add_parser("season", help="ingest every completed round of a season")
    season.add_argument("--year", type=int, required=True)
    season.add_argument("--sessions", default="Q,R", help="comma-separated codes (default Q,R)")
    season.add_argument("--force", action="store_true")

    events = sub.add_parser("events", help="list (and cache) a season's schedule")
    events.add_argument("--year", type=int, required=True)
    events.add_argument("--refresh", action="store_true")

    listing = sub.add_parser("list", help="list stored sessions")
    listing.add_argument("--year", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _configure_logging(args.verbose)
    repo = ParquetRepository(args.data_dir)
    service = IngestionService(repo, FastF1Client(args.cache_dir))

    try:
        if args.command == "ingest":
            result = service.ingest(args.year, args.round, args.session, force=args.force)
            state = "skipped (already stored)" if result.skipped else (
                f"{result.laps} laps, {result.telemetry_laps} with telemetry, {result.valid_laps} valid"
            )
            print(f"{result.key}: {state} in {result.elapsed_s:.1f}s")
        elif args.command == "season":
            results = service.ingest_season(args.year, args.sessions.split(","), force=args.force)
            for result in results:
                state = f"FAILED: {result.error}" if result.error else ("skipped" if result.skipped else f"{result.laps} laps")
                print(f"{result.key}: {state}")
            return 1 if any(result.error for result in results) else 0
        elif args.command == "events":
            for event in service.list_events(args.year, refresh=args.refresh):
                codes = ",".join(s.code or s.name for s in event.sessions)
                print(f"R{event.round:02d} {event.event_name} ({event.location}) [{event.event_format}] {codes}")
        elif args.command == "list":
            for info in repo.list_sessions(args.year):
                print(f"{info.key}  {info.event_name}  {info.session_name}  {info.date:%Y-%m-%d}")
    except IngestionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
