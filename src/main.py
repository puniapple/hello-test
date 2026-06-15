"""Application entry point."""

import asyncio
import logging

import structlog

from src.config import settings


def configure_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ]
    )


async def main() -> None:
    configure_logging()
    log = structlog.get_logger()
    log.info("starting", environment=settings.environment)
    log.info("phase_1_skeleton_ok")


if __name__ == "__main__":
    asyncio.run(main())
