"""Streaming ASGI request body size limit middleware (Prompt 08.2 security hardening).

Never reads an unbounded request body into RAM: inspects `Content-Length` upfront and
counts streamed ASGI `http.request` body chunks incrementally, immediately returning
HTTP 413 with the standard JSON ErrorEnvelope once `max_body_bytes` is exceeded.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _PayloadTooLargeError(Exception):
    """Internal sentinel raised by wrapped ASGI receive when body exceeds limit."""


class MaxBodySizeMiddleware:
    """Pure ASGI middleware enforcing a hard ceiling on incoming HTTP request bytes."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = 1_048_576) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # 1. Fast-path check on Content-Length header before reading any body bytes
        for raw_key, raw_val in scope.get("headers", ()):
            if raw_key.lower() == b"content-length":
                try:
                    declared = int(raw_val.decode("ascii").strip())
                except (UnicodeDecodeError, ValueError):
                    await self._send_json_error(
                        send,
                        status_code=400,
                        code="BAD_REQUEST",
                        message="Invalid Content-Length header",
                    )
                    return
                if declared > self._max_body_bytes:
                    await self._send_too_large(send)
                    return
                break

        # 2. Incremental streaming byte counter for chunked or un-declared request bodies
        received_bytes = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                received_bytes += len(chunk)
                if received_bytes > self._max_body_bytes:
                    raise _PayloadTooLargeError
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, bounded_receive, tracked_send)
        except _PayloadTooLargeError:
            if not response_started:
                await self._send_too_large(send)

    async def _send_too_large(self, send: Send) -> None:
        await self._send_json_error(
            send,
            status_code=413,
            code="PAYLOAD_TOO_LARGE",
            message=(
                f"Request payload exceeds maximum allowed size ({self._max_body_bytes} bytes)"
            ),
            details={"max_request_body_bytes": self._max_body_bytes},
        )

    @staticmethod
    async def _send_json_error(
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "details": details,
                    "request_id": None,
                }
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})
