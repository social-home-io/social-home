"""Theme domain types (§23.123, §23.125)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class HouseholdTheme:
    """Household-wide visual preferences."""

    # Brand-aligned defaults — hearth terracotta + honey, matching
    # ``tokens.css``'s ``--sh-primary`` / ``--sh-warning``.
    primary_color: str = "#D2542A"
    accent_color: str = "#C8902F"
    surface_color: str | None = None
    surface_dark: str | None = None
    mode: str = "auto"
    font_family: str = "system"
    density: str = "comfortable"
    corner_radius: int = 12
    updated_at: str | None = None


@dataclass(slots=True, frozen=True)
class SpaceTheme:
    """Per-space overrides (§23.123)."""

    space_id: str
    # Brand-aligned defaults — hearth terracotta + honey, matching
    # ``tokens.css``'s ``--sh-primary`` / ``--sh-warning``.
    primary_color: str = "#D2542A"
    accent_color: str = "#C8902F"
    header_image_file: str | None = None
    background_tint: str | None = None
    mode_override: str | None = None
    font_family: str = "system"
    post_layout: str = "card"
    updated_at: str | None = None
