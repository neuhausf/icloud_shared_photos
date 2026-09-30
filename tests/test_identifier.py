"""Tests for media source identifiers."""

from __future__ import annotations

import pytest

from isp.identifier import InvalidIdentifier, PhotosIdentifier


@pytest.mark.parametrize(
    "identifier",
    [
        PhotosIdentifier(entry_id="abc"),
        PhotosIdentifier(entry_id="abc", view="lib", zone="SharedSync-1234-ABCD"),
        PhotosIdentifier(
            entry_id="abc", view="lib", zone="PrimarySync", album="Favorites"
        ),
        PhotosIdentifier(
            entry_id="abc",
            view="lib",
            zone="SharedSync-1",
            album="Favorites",
            photo_id="AY/+x=",
        ),
        PhotosIdentifier(entry_id="abc", view="fav"),
        PhotosIdentifier(entry_id="abc", view="fav", collection="all"),
    ],
)
def test_roundtrip(identifier: PhotosIdentifier) -> None:
    """Identifiers survive serialization, including special characters."""
    assert PhotosIdentifier.parse(str(identifier)) == identifier


def test_photo_id_is_quoted() -> None:
    """Slashes in record names do not create extra path segments."""
    ident = PhotosIdentifier(
        entry_id="e", view="lib", zone="z", album="Library", photo_id="a/b"
    )
    assert str(ident) == "e/lib/z/Library/a%2Fb"
    assert ident.is_photo


@pytest.mark.parametrize(
    "raw",
    ["", "e/unknown", "e/lib", "e/lib/z/a/p/extra", "e/fav/bogus", "e/fav/all/x"],
)
def test_invalid(raw: str) -> None:
    """Malformed identifiers are rejected."""
    with pytest.raises(InvalidIdentifier):
        PhotosIdentifier.parse(raw)
