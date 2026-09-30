"""Media source identifiers for iCloud Shared Photos.

Kept free of Home Assistant imports so it can be unit tested in isolation.

Identifier layout (each segment is URL-quoted)::

    <entry_id>                                  account root
    <entry_id>/lib/<zone>                       albums of one library (zone)
    <entry_id>/lib/<zone>/<album>               photos of one album
    <entry_id>/lib/<zone>/<album>/<photo_id>    a single photo
    <entry_id>/fav                              favorites collections
    <entry_id>/fav/<collection>                 photos of a favorites collection

``<entry_id>`` is the config entry id of the *core* ``icloud`` integration,
``<zone>`` the CloudKit zone name (``PrimarySync`` / ``SharedSync-<UUID>``).
Photos listed inside a favorites collection always carry the ``lib`` form so
that every photo can be resolved against its own zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote

from .const import FAVORITES_COLLECTIONS

VIEW_LIBRARY = "lib"
VIEW_FAVORITES = "fav"


class InvalidIdentifier(ValueError):
    """Raised when a media source identifier cannot be parsed."""


@dataclass(frozen=True, kw_only=True)
class PhotosIdentifier:
    """Parsed media source identifier."""

    entry_id: str
    view: str | None = None
    zone: str | None = None
    album: str | None = None
    photo_id: str | None = None
    collection: str | None = None

    @classmethod
    def parse(cls, identifier: str) -> PhotosIdentifier:
        """Parse an identifier string."""
        parts = [unquote(part) for part in identifier.split("/")] if identifier else []
        if not parts or not parts[0]:
            raise InvalidIdentifier("Missing account in identifier")
        entry_id = parts[0]
        if len(parts) == 1:
            return cls(entry_id=entry_id)

        view = parts[1]
        if view == VIEW_LIBRARY:
            if not 3 <= len(parts) <= 5 or not all(parts[2:]):
                raise InvalidIdentifier(f"Invalid library identifier: {identifier}")
            return cls(
                entry_id=entry_id,
                view=view,
                zone=parts[2],
                album=parts[3] if len(parts) > 3 else None,
                photo_id=parts[4] if len(parts) > 4 else None,
            )
        if view == VIEW_FAVORITES:
            if len(parts) > 3:
                raise InvalidIdentifier(f"Invalid favorites identifier: {identifier}")
            collection = parts[2] if len(parts) == 3 else None
            if collection is not None and collection not in FAVORITES_COLLECTIONS:
                raise InvalidIdentifier(f"Unknown favorites collection: {collection}")
            return cls(entry_id=entry_id, view=view, collection=collection)
        raise InvalidIdentifier(f"Unknown view '{view}' in identifier")

    def __str__(self) -> str:
        """Serialize the identifier."""
        parts: list[str | None] = [self.entry_id, self.view]
        if self.view == VIEW_LIBRARY:
            parts += [self.zone, self.album, self.photo_id]
        elif self.view == VIEW_FAVORITES:
            parts.append(self.collection)
        return "/".join(quote(part, safe="") for part in parts if part is not None)

    @property
    def is_photo(self) -> bool:
        """Return True if the identifier points to a single photo."""
        return self.view == VIEW_LIBRARY and self.photo_id is not None
