"""Fetch + parse the remote app catalog published by ``socialhome-apps``.

The catalog is a JSON document ``{"apps": [<AppCatalogEntry>, ...]}``
served from a GitHub release asset. This service only *reads* it; install
(download + verify + unpack) lives in :class:`AppService`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any


from ..domain.apps import AppCatalogEntry

log = logging.getLogger(__name__)


class AppCatalogService:
    """Fetch and parse the remote app catalog."""

    __slots__ = ("_session_factory", "_catalog_url")

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        catalog_url: str,
    ) -> None:
        """Initialize the service.

        Args:
            session_factory: A callable that returns an aiohttp.ClientSession.
            catalog_url: The URL of the catalog.json to fetch.
        """
        self._session_factory = session_factory
        self._catalog_url = catalog_url

    async def fetch_catalog(self) -> list[AppCatalogEntry]:
        """GET the catalog and parse it.

        Malformed entries are skipped (logged at WARNING) so one bad entry
        can't break Browse.

        Returns:
            List of valid AppCatalogEntry objects.
        """
        session = self._session_factory()
        async with session.get(self._catalog_url) as resp:
            resp.raise_for_status()
            raw = await resp.text()
        doc = json.loads(raw)
        out: list[AppCatalogEntry] = []
        for item in doc.get("apps", []):
            try:
                out.append(AppCatalogEntry.from_dict(item))
            except (ValueError, TypeError) as exc:
                log.warning("skipping malformed catalog entry %r: %s", item, exc)
        return out
