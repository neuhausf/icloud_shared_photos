"""Tests for the pyicloud access layer.

The end-to-end tests drive the *real* pyicloud ``PhotosService`` (as pinned by
Home Assistant) with a fake HTTP session that emulates the CloudKit endpoints,
so they verify our assumptions about pyicloud's Shared Library support.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from isp.library import (
    AccountPhotos,
    PhotoNotFoundError,
    PhotosUnavailableError,
    merge_favorites,
)

from .fake_icloud import (
    COLLECTION_ZONE,
    PRIMARY_ZONE_NAME,
    SHARED_ZONE,
    _fake_post,
    _json_response,
)

pyicloud_photos = pytest.importorskip("pyicloud.services.photos")


@pytest.fixture
def api() -> MagicMock:
    """Return a fake PyiCloudService with a real pyicloud PhotosService."""
    session = MagicMock()
    session.post.side_effect = _fake_post
    service = pyicloud_photos.PhotosService(
        "https://photos.example",
        session,
        {"dsid": "123"},
        "https://upload.example",
        "https://streams.example",
    )
    fake_api = MagicMock()
    fake_api.requires_2fa = False
    fake_api.requires_2sa = False
    fake_api.photos = service
    return fake_api


def test_discovers_shared_library_zone(api: MagicMock) -> None:
    """PrimarySync and SharedSync-* are found; other zones/streams are ignored."""
    libraries = AccountPhotos("test").libraries(api)
    assert set(libraries) == {PRIMARY_ZONE_NAME, SHARED_ZONE, COLLECTION_ZONE}
    assert libraries[SHARED_ZONE].shared
    assert libraries[COLLECTION_ZONE].collection
    assert not libraries[COLLECTION_ZONE].shared
    assert not libraries[PRIMARY_ZONE_NAME].shared
    assert libraries[SHARED_ZONE].library.zone_id["zoneName"] == SHARED_ZONE


def test_shared_favorites_come_from_shared_zone(api: MagicMock) -> None:
    """Shared Library -> Favorites lists only the SharedSync zone's favorites."""
    account = AccountPhotos("test")
    refs = account.list_album(
        api, SHARED_ZONE, "Favorites", max_items=50, include_videos=False, ttl=60
    )
    assert [ref.photo_id for ref in refs] == ["S1", "S2"]
    assert {ref.zone for ref in refs} == {SHARED_ZONE}
    assert refs[0].photo.filename == "shared-fav.heic"
    # every POST that listed photos targeted the shared zone
    zones = {
        call.kwargs["json"]["zoneID"]["zoneName"]
        for call in api.photos.session.post.call_args_list
        if call.kwargs.get("json", {}).get("query", {}).get("recordType")
        == "CPLAssetAndMasterInSmartAlbumByAssetDate"
    }
    assert zones == {SHARED_ZONE}


def test_shared_library_album(api: MagicMock) -> None:
    """Shared Library -> Library lists all shared photos."""
    refs = AccountPhotos("test").list_album(
        api, SHARED_ZONE, "Library", max_items=50, include_videos=False, ttl=60
    )
    assert {ref.photo_id for ref in refs} == {"S1", "S2", "S3"}


def test_max_items(api: MagicMock) -> None:
    """The listing is capped."""
    refs = AccountPhotos("test").list_album(
        api, SHARED_ZONE, "Library", max_items=2, include_videos=False, ttl=60
    )
    assert len(refs) == 2


def test_all_favorites_deduplicated(api: MagicMock) -> None:
    """All Favorites merges personal + shared newest-first without duplicates."""
    account = AccountPhotos("test")
    kwargs = {"max_items": 50, "include_videos": False, "ttl": 60}
    assert [r.photo_id for r in account.list_favorites(api, "personal", **kwargs)] == [
        "P2",
        "P1",
    ]
    assert [r.photo_id for r in account.list_favorites(api, "shared", **kwargs)] == [
        "S1",
        "S2",
    ]
    merged = account.list_favorites(api, "all", **kwargs)
    assert [r.photo_id for r in merged] == ["S1", "S2", "P1"]


def test_listing_is_cached(api: MagicMock) -> None:
    """A second listing within the TTL does not hit iCloud again."""
    account = AccountPhotos("test")
    kwargs = {"max_items": 50, "include_videos": False, "ttl": 60}
    account.list_album(api, SHARED_ZONE, "Favorites", **kwargs)
    calls = api.photos.session.post.call_count
    account.list_album(api, SHARED_ZONE, "Favorites", **kwargs)
    assert api.photos.session.post.call_count == calls


def test_get_photo_and_download_urls(api: MagicMock) -> None:
    """A photo is resolved from cache or by direct lookup, with its URLs."""
    account = AccountPhotos("test")
    photo = account.get_photo(api, SHARED_ZONE, "Favorites", "S1")
    assert photo.id == "S1"
    assert photo.resources["medium"].url == "https://cdn.example/S1/med"
    assert photo.resources["original"].type == "public.heic"
    # refresh forces a new lookup
    calls = api.photos.session.post.call_count
    account.get_photo(api, SHARED_ZONE, "Favorites", "S1", refresh=True)
    assert api.photos.session.post.call_count > calls


def test_unknown_photo_and_zone(api: MagicMock) -> None:
    """Unknown ids raise PhotoNotFoundError."""
    account = AccountPhotos("test")
    with pytest.raises(PhotoNotFoundError):
        account.get_photo(api, SHARED_ZONE, "Favorites", "nope")
    with pytest.raises(PhotoNotFoundError):
        account.list_album(
            api, "SharedSync-x", "Favorites", max_items=1, include_videos=False, ttl=0
        )
    with pytest.raises(PhotoNotFoundError):
        account.list_album(
            api, SHARED_ZONE, "Hidden", max_items=1, include_videos=False, ttl=0
        )


def test_no_shared_library(api: MagicMock) -> None:
    """Accounts without a Shared Library are not an error."""
    service = api.photos
    original = _fake_post

    def post(url: str, json: dict[str, Any] | None = None, **kw: Any) -> MagicMock:
        if "zones/list" in url:
            return _json_response(
                {"zones": [{"zoneID": {"zoneName": PRIMARY_ZONE_NAME}}]}
            )
        return original(url, json=json, **kw)

    service.session.post.side_effect = post
    account = AccountPhotos("test")
    assert account.shared_zones(api) == []
    assert (
        account.list_favorites(api, "shared", max_items=10, include_videos=False, ttl=0)
        == []
    )
    assert (
        len(
            account.list_favorites(
                api, "all", max_items=10, include_videos=False, ttl=0
            )
        )
        == 2
    )


def test_unauthenticated() -> None:
    """Missing or 2FA-pending sessions raise PhotosUnavailableError."""
    account = AccountPhotos("test")
    with pytest.raises(PhotosUnavailableError):
        account.libraries(None)
    api = MagicMock()
    api.requires_2fa = True
    with pytest.raises(PhotosUnavailableError):
        account.libraries(api)


def test_merge_favorites_without_fingerprints() -> None:
    """Items without fingerprint are only de-duplicated by zone + id."""
    photo = MagicMock()
    photo.id = "X"
    photo.resources = {}
    from isp.library import PhotoRef

    refs = [PhotoRef(zone="a", album="Favorites", photo=photo)]
    assert len(merge_favorites([refs, refs])) == 1
    other = [PhotoRef(zone="b", album="Favorites", photo=photo)]
    assert len(merge_favorites([refs, other])) == 2


def test_shared_albums(api: MagicMock) -> None:
    """Legacy Shared Albums are listed and found by title or id."""
    from isp.const import SHARED_ALBUMS_ZONE

    from .fake_icloud import SHARED_ALBUM_GUID

    account = AccountPhotos("test")
    albums = account.shared_albums(api, ttl=60)
    assert [info.title for info in albums.values()] == ["Bilderrahmen", "Fotorahmen"]
    assert account.find_shared_album(api, "bilderrahmen ", ttl=60).album_id == (
        SHARED_ALBUM_GUID
    )
    assert account.find_shared_album(api, SHARED_ALBUM_GUID, ttl=60).title == (
        "Bilderrahmen"
    )
    with pytest.raises(PhotoNotFoundError, match="available: Bilderrahmen, Fotorahmen"):
        account.find_shared_album(api, "Ferien", ttl=60)

    refs = account.list_album(
        api,
        SHARED_ALBUMS_ZONE,
        SHARED_ALBUM_GUID,
        max_items=50,
        include_videos=False,
        ttl=60,
    )
    assert [ref.photo_id for ref in refs] == ["A1", "A2"]
    assert {ref.zone for ref in refs} == {SHARED_ALBUMS_ZONE}

    from isp.library import photo_resources

    photo = account.get_photo(
        api, SHARED_ALBUMS_ZONE, SHARED_ALBUM_GUID, "A1", refresh=True
    )
    resources = photo_resources(photo)
    assert resources["medium"].url == "https://cdn.example/A1/med"
    assert resources["original"].type == "public.heic"
    with pytest.raises(PhotoNotFoundError):
        account.get_photo(api, SHARED_ALBUMS_ZONE, "unknown-guid", "Z9")


def test_new_shared_album_is_found(api: MagicMock) -> None:
    """An album shared after the first listing is found without restart."""
    from . import fake_icloud

    account = AccountPhotos("test")
    assert [i.title for i in account.shared_albums(api, ttl=3600).values()] == [
        "Bilderrahmen",
        "Fotorahmen",
    ]
    new_album = {
        **fake_icloud.SHARED_ALBUMS[0],
        "albumguid": "NEW-GUID",
        "attributes": {
            **fake_icloud.SHARED_ALBUMS[0]["attributes"],
            "name": "Ferien 2026",
        },
    }
    fake_icloud.SHARED_ALBUMS.append(new_album)
    try:
        assert account.find_shared_album(api, "Ferien 2026", ttl=3600).album_id == (
            "NEW-GUID"
        )
    finally:
        fake_icloud.SHARED_ALBUMS.remove(new_album)


def test_cloudkit_shared_album(api: MagicMock) -> None:
    """A SharedCollection zone is a Shared Album titled by its album record."""
    account = AccountPhotos("test")
    info = account.find_shared_album(api, "fotorahmen", ttl=60)
    assert info.album_id == COLLECTION_ZONE
    assert info.zone == COLLECTION_ZONE
    assert info.source_album == "Library"
    refs = account.list_album(
        api,
        info.zone,
        info.source_album,
        max_items=50,
        include_videos=False,
        ttl=60,
    )
    assert [ref.photo_id for ref in refs] == ["C1"]
    assert account.find_shared_album(api, COLLECTION_ZONE, ttl=60).title == (
        "Fotorahmen"
    )


def test_collection_title_fallback() -> None:
    """Without exactly one album record the zone gives a placeholder title."""
    from isp.library import LibraryInfo, _collection_title

    library = MagicMock()
    library.albums = []
    info = LibraryInfo(
        zone=COLLECTION_ZONE, shared=False, library=library, collection=True
    )
    assert _collection_title(info) == "Shared Album E78388A3"


def test_inspect_collection_summarizes_records() -> None:
    """The inspector reports types, field names and decoded titles only."""
    import base64

    from isp.library import LibraryInfo, _inspect_collection

    library = MagicMock()
    library.zone_id = {"zoneName": COLLECTION_ZONE}
    library.scope = "private"
    library._client._client._http.post.return_value = {
        "zones": [
            {
                "moreComing": False,
                "records": [
                    {
                        "recordType": "CPLAlbum",
                        "fields": {
                            "albumNameEnc": {
                                "type": "ENCRYPTED_BYTES",
                                "value": base64.b64encode(b"Fotorahmen").decode(),
                            },
                            "url": {
                                "type": "STRING",
                                "value": "https://cdn.example/secret",
                            },
                        },
                    },
                    {
                        "recordType": "CPLMaster",
                        "fields": {
                            "filenameEnc": {"type": "STRING", "value": "SU1HLkhFSUM="}
                        },
                    },
                ],
            }
        ]
    }
    account = MagicMock()
    account.list_album.return_value = []
    info = LibraryInfo(
        zone=COLLECTION_ZONE, shared=False, library=library, collection=True
    )
    report = _inspect_collection(account, MagicMock(), info, 10)
    assert report["record_types"] == {"CPLAlbum": 1, "CPLMaster": 1}
    assert report["fields"]["CPLAlbum"] == {
        "albumNameEnc": "ENCRYPTED_BYTES",
        "url": "STRING",
    }
    assert report["text_values"] == {"CPLAlbum": {"albumNameEnc": ["Fotorahmen"]}}
    assert report["library_items"] == 0
