"""Shared validation bounds for human Task search."""

MAX_SEARCH_LIMIT = 50
MAX_SEARCH_QUERY_BYTES = 256


class SearchError(RuntimeError):
    """Raised when a bounded Task search or dashboard query is invalid."""
