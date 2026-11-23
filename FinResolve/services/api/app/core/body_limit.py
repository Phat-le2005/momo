"""Request body size limit middleware (Prompt 08 security hardening)."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.errors import ErrorCode, error_response


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject HTTP requests whose payload exceeds `max_body_bytes` with 413 JSON envelope."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = 1_048_576) -> None:
        super().__init__(app)
        self._max_body_bytes = max_body_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_body_bytes:
                    return self._too_large_response(request)
            except ValueError:
                return error_response(
                    request,
                    400,
                    ErrorCode.BAD_REQUEST.value,
                    "Invalid Content-Length header",
                )

        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if len(body) > self._max_body_bytes:
                return self._too_large_response(request)

        return await call_next(request)

    def _too_large_response(self, request: Request) -> JSONResponse:
        return error_response(
            request,
            413,
            "PAYLOAD_TOO_LARGE",
            f"Request payload exceeds maximum allowed size ({self._max_body_bytes} bytes)",
            details={"max_request_body_bytes": self._max_body_bytes},
        )
