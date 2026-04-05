import logging
import structlog


def setup_logging(debug: bool = False) -> None:
    """Configure structured JSON logging for the application.

    Sets up structlog to output JSON-formatted log lines with consistent
    fields across all log calls.

    Args:
        debug (bool): If True, use pretty console output instead of JSON.
            Should match the DEBUG environment variable. Defaults to False.

    Returns:
        None
    """
    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if debug:
        # Readable output for local development
        renderer = structlog.dev.ConsoleRenderer()
    else:
        # Readable JSON for production
        renderer = structlog.processors.JSONRenderer()

    # Silence noisy httpx and httpcore debug logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Set the standard library logging level
    logging.basicConfig(
        format="%(message)s",
        level=logging.DEBUG if debug else logging.INFO,
    )
