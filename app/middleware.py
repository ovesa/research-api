import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware that adds correlation IDs and logs every request. Runs
    on every incoming HTTP request before it reaches any endpoint. Generates
    a unique request ID, attaches it to the response headers, and logs the
    request start and completion with timing information. The request ID is
    threaded through all log lines produced during that request using structlog's
    context binding. This means you can filter logs by request_id to see the entire
    lifecycle of one request.

    Example log output:
        {
            "event": "request_started",
            "request_id": "a3f9b2c1-...",
            "method": "POST",
            "path": "/papers/lookup",
            "timestamp": "2026-04-05T18:52:35Z"
        }
        {
            "event": "request_complete",
            "request_id": "a3f9b2c1-...",
            "method": "POST",
            "path": "/papers/lookup",
            "status_code": 200,
            "duration_ms": 342.1
        }
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        """Process a request, adding correlation ID and timing.

        Args:
            request (Request): The incoming HTTP request.
            call_next: The next middleware or endpoint handler to call.

        Returns:
            Response: The HTTP response with X-Request-ID header added.
        """
        # Generate a unique ID for this request
        request_id = str(uuid.uuid4())

        # Bind request_id to all log lines produced during this request
        log = logger.bind(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        start_time = time.perf_counter()
        log.info("request_started")

        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000

            log.info(
                "request_complete",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )

            # Add request ID to response headers so clients can reference it
            response.headers["X-Request-ID"] = request_id
            return response

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            log.error(
                "request_failed",
                error=str(e),
                duration_ms=round(duration_ms, 2),
            )
            raise
