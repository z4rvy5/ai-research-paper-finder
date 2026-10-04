"""Errors that map directly onto the API's `{"error": {code, message, retryable}}` envelope."""


class ApiError(Exception):
    """A request problem with a fixed HTTP status and a stable, safe-to-show code and message."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.headers = headers  # e.g. Retry-After
