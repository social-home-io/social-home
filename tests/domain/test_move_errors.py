"""Tests for socialhome.domain.move_errors."""

from __future__ import annotations

from socialhome.domain.move_errors import StaleMoveLink


def test_stale_move_link_is_exception_subclass():
    """StaleMoveLink exists and is a plain Exception subclass the service can
    raise + the route layer can map."""
    assert issubclass(StaleMoveLink, Exception)
    err = StaleMoveLink("older link")
    assert isinstance(err, Exception)
    assert str(err) == "older link"
