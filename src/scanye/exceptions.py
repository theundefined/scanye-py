class ScanyeError(Exception):
    """Base exception for Scanye API."""


class ScanyeAuthError(ScanyeError):
    """Exception raised for authentication errors."""


class ScanyeRequestError(ScanyeError):
    """Exception raised for network or request errors."""
