"""One error shape for every /v1 response.

    {
      "error": {
        "code":       "validation_error",      machine-readable, stable
        "message":    "2 problem(s) with this client.",
        "status":     422,
        "problems":   [{"field": "...", "message": "...", "hint": "..."}],
        "request_id": "9f2c..."               quote this when reporting
      }
    }

The legacy routes grew three different 422 bodies (validation_error,
client_invalid, booking_request_invalid) and mixed string and object `detail`.
A web app then needs one parser per endpoint. /v1 normalises every error —
including ones raised by the legacy handlers it reuses — into this envelope,
so the client parses errors exactly one way.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Request
from pydantic import BaseModel, Field

V1_PREFIX = "/v1"

#: Status code -> default machine code, when the raiser gave none.
DEFAULT_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden",
    404: "not_found", 405: "method_not_allowed", 409: "conflict",
    413: "payload_too_large", 422: "validation_error", 423: "locked",
    429: "rate_limited", 500: "internal_error", 503: "service_unavailable",
}

#: Legacy error names folded into one code each, so /v1 has no synonyms.
CODE_ALIASES = {
    "client_invalid": "validation_error",
    "booking_request_invalid": "validation_error",
}


class Problem(BaseModel):
    field: str = ""
    message: str
    severity: str = "error"
    hint: str = ""


class ErrorBody(BaseModel):
    code: str
    message: str
    status: int
    problems: List[Problem] = Field(default_factory=list)
    request_id: str = ""


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class ApiError(Exception):
    """Raise from /v1 code for a clean, typed error response."""

    def __init__(self, status: int, message: str, code: str = "",
                 problems: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or DEFAULT_CODES.get(status, "error")
        self.problems = problems or []


def is_v1(request: Request) -> bool:
    return request.url.path.startswith(V1_PREFIX + "/") or request.url.path == V1_PREFIX


def envelope(request: Request, status: int, message: str, code: str = "",
             problems: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    code = CODE_ALIASES.get(code, code) or DEFAULT_CODES.get(status, "error")
    clean = []
    for p in problems or []:
        if isinstance(p, dict):
            clean.append({k: v for k, v in {
                "field": str(p.get("field", "")),
                "message": str(p.get("message", "")),
                "severity": str(p.get("severity", "error")),
                "hint": str(p.get("hint", "")),
            }.items()})
    return {"error": {
        "code": code,
        "message": message,
        "status": status,
        "problems": clean,
        "request_id": getattr(request.state, "request_id", ""),
    }}


def from_detail(request: Request, status: int, detail: Any) -> Dict[str, Any]:
    """Normalise a legacy HTTPException.detail (str or dict) into /v1 form."""
    if isinstance(detail, dict):
        return envelope(request, status,
                        str(detail.get("detail") or detail.get("message") or ""),
                        str(detail.get("error") or ""),
                        detail.get("problems"))
    return envelope(request, status, str(detail))


#: For OpenAPI `responses=` on /v1 routes.
ERROR_RESPONSES = {code: {"model": ErrorEnvelope}
                   for code in (401, 404, 409, 422, 429, 500)}
