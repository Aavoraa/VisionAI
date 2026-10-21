"""OpenAPI documentation shared by the route modules: error-response examples and headers.

Example messages mirror what the API really returns; tests compare several of them with
live responses so the docs cannot silently drift from behaviour.
"""

from typing import Any

from app.schemas.vision import VisionErrorResponse

EXAMPLE_REQUEST_ID = "0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e"
EXAMPLE_JOB_ID = "8d0c6f4e-2b1a-4c3d-9e8f-7a6b5c4d3e2f"

# name -> (summary shown in Swagger's example dropdown, error type, message, param)
ErrorExamples = dict[str, tuple[str, str, str, str | None]]


def error_body(error_type: str, message: str, param: str | None = None) -> dict[str, Any]:
    return {"error": {"type": error_type, "message": message, "param": param}, "request_id": EXAMPLE_REQUEST_ID}


def error_response(
    description: str, examples: ErrorExamples, headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A ``responses`` entry: the standard error schema plus named examples."""
    doc: dict[str, Any] = {
        "model": VisionErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "examples": {
                    name: {"summary": summary, "value": error_body(error_type, message, param)}
                    for name, (summary, error_type, message, param) in examples.items()
                }
            }
        },
    }
    if headers:
        doc["headers"] = headers
    return doc


def _int_header(description: str) -> dict[str, Any]:
    return {"description": description, "schema": {"type": "integer"}}


REQUEST_ID_HEADER = {
    "X-Request-ID": {
        "description": "Request id (echoes a valid incoming `X-Request-ID`, otherwise generated).",
        "schema": {"type": "string"},
    }
}
RATE_LIMIT_HEADERS = {
    "X-RateLimit-Limit": _int_header("Requests allowed per window."),
    "X-RateLimit-Remaining": _int_header("Requests left in the current window."),
    "X-RateLimit-Reset": _int_header("Seconds until the current window resets."),
}

UNAUTHORIZED = error_response(
    "Missing, malformed, unknown or revoked API key.",
    {
        "missing_key": ("No Authorization header", "authentication_error", "Missing API key", None),
        "invalid_key": ("Unknown or malformed key", "authentication_error", "Invalid API key", None),
        "revoked_key": ("Revoked key", "authentication_error", "API key has been revoked", None),
    },
    headers={"WWW-Authenticate": {"description": "Always `Bearer`.", "schema": {"type": "string"}}},
)

NOT_SIGNED_IN = error_response(
    "No valid session cookie.",
    {
        "not_signed_in": ("No or unknown session", "authentication_error", "Not signed in", None),
        "expired": ("Session expired", "authentication_error", "Session expired; please sign in again", None),
    },
)
SESSION_FORBIDDEN = error_response(
    "Signed in, but the request is not allowed.",
    {
        "csrf": ("Missing or wrong X-CSRF-Token", "permission_error", "Missing or invalid CSRF token", None),
        "inactive": ("Account deactivated", "permission_error", "User account is inactive", None),
    },
)
ADMIN_FORBIDDEN = error_response(
    "Signed in, but not allowed: not an administrator, missing CSRF token, or deactivated.",
    {
        "not_admin": ("Regular user", "permission_error", "Administrator access required", None),
        "csrf": ("Missing or wrong X-CSRF-Token", "permission_error", "Missing or invalid CSRF token", None),
        "inactive": ("Account deactivated", "permission_error", "User account is inactive", None),
    },
)


def invalid_id_response(param: str) -> dict[str, Any]:
    """422 for a malformed UUID path parameter."""
    from app.schemas.common import UUID_PATTERN

    return error_response(
        f"`{param}` is not a UUID.",
        {
            "malformed_id": (
                "Not a UUID",
                "invalid_request_error",
                f"{param}: String should match pattern '{UUID_PATTERN}'",
                param,
            )
        },
    )


def not_found_response(what: str) -> dict[str, Any]:
    return error_response(
        f"No such {what.lower()} (or it belongs to someone else).",
        {"not_found": (f"Unknown {what.lower()}", "not_found_error", f"{what} not found", None)},
    )


FORBIDDEN = error_response(
    "The key is valid but may not be used.",
    {
        "expired_key": ("Expired key", "permission_error", "API key has expired", None),
        "inactive_user": ("Deactivated account", "permission_error", "User account is inactive", None),
    },
)
