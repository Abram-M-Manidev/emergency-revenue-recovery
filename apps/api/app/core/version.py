"""Single source of truth for the API version string.

Bump on release; surfaced via GET /api/v1/version and included in the
OpenAPI schema so clients can detect skew against the backend they expect.
"""

APP_VERSION = "0.1.0"
