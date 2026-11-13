"""HTTP runtime concerns: request correlation, auth, logging, and errors."""
import logging
import re
import secrets
import time
import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobforge.observability import configure_logging
from jobforge.settings import get_settings


REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}

logger = logging.getLogger("jobforge.http")


def _request_id(request: Request) -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    return supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex


def _provided_api_key(request: Request) -> str:
    key = request.headers.get("X-API-Key", "").strip()
    if key:
        return key
    authorization = request.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer":
        return value.strip()
    return ""


def _api_key_valid(request: Request, expected: str) -> bool:
    supplied = _provided_api_key(request)
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    details: Optional[Any] = None,
    legacy_detail: Optional[Any] = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "") or _request_id(request)
    request.state.request_id = request_id
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
    }
    if details is not None:
        error["details"] = details
    body = {
        "detail": legacy_detail if legacy_detail is not None else message,
        "error": error,
    }
    response_headers = dict(headers or {})
    response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(status_code=status_code, content=body, headers=response_headers)


def install_http_runtime(app: FastAPI) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
                "type": error.get("type", "validation_error"),
                "message": error.get("msg", "invalid value"),
            }
            for error in exc.errors()
        ]
        return _error_response(
            request,
            422,
            "validation_error",
            "Request validation failed",
            details=details,
            legacy_detail="Request validation failed",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code") or _STATUS_CODES.get(exc.status_code, "http_error"))
            message = str(detail.get("message") or detail.get("detail") or "Request failed")
        else:
            code = _STATUS_CODES.get(exc.status_code, "http_error")
            message = str(detail or "Request failed")
        return _error_response(
            request,
            exc.status_code,
            code,
            message,
            legacy_detail=detail,
            headers=getattr(exc, "headers", None),
        )

    @app.middleware("http")
    async def operations_middleware(request: Request, call_next):
        request.state.request_id = _request_id(request)
        settings_now = get_settings()
        path = request.url.path
        started = time.perf_counter()

        if (settings_now.api_key
                and (path == "/api" or path.startswith("/api/"))
                and not _api_key_valid(request, settings_now.api_key)):
            response = _error_response(
                request,
                401,
                "unauthorized",
                "API key required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "http.request_failed",
                    extra={
                        "event": "http.request_failed",
                        "request_id": request.state.request_id,
                        "method": request.method,
                        "path": path,
                    },
                )
                response = _error_response(
                    request,
                    500,
                    "internal_error",
                    "Internal server error",
                )

        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "http.request_completed",
            extra={
                "event": "http.request_completed",
                "request_id": request.state.request_id,
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response
