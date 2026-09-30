"""End-to-end test: browse, resolve and stream a Shared Library favorite."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import media_source
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.icloud_shared_photos.const import DOMAIN

from tests.fake_icloud import SHARED_ZONE, _fake_post

pyicloud_photos = pytest.importorskip("pyicloud.services.photos")


@pytest.fixture
def fake_api() -> MagicMock:
    """Fake PyiCloudService with a real pyicloud PhotosService."""
    session = MagicMock()
    session.post.side_effect = _fake_post
    service = pyicloud_photos.PhotosService(
        "https://photos.example",
        session,
        {"dsid": "123"},
        "https://upload.example",
        "https://streams.example",
    )
    api = MagicMock()
    api.requires_2fa = False
    api.requires_2sa = False
    api.photos = service
    return api


@pytest.fixture
async def setup(hass: HomeAssistant, fake_api: MagicMock) -> MockConfigEntry:
    """Set up a fake loaded core iCloud entry and this integration."""
    assert await async_setup_component(hass, "http", {})
    with patch("homeassistant.components.icloud.async_setup", return_value=True):
        icloud_entry = MockConfigEntry(
            domain="icloud", title="me@example.com", unique_id="me@example.com"
        )
        icloud_entry.add_to_hass(hass)
        icloud_entry.mock_state(hass, ConfigEntryState.LOADED)
        icloud_entry.runtime_data = SimpleNamespace(
            api=fake_api, cancel_fetch=lambda: None
        )

        entry = MockConfigEntry(domain=DOMAIN, title="iCloud Shared Photos")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "media_source", {})
        await hass.async_block_till_done()
    return icloud_entry


async def test_browse_resolve_and_stream(
    hass: HomeAssistant, setup: MockConfigEntry, hass_client, aioclient_mock
) -> None:
    """Media → iCloud Shared Photos → Shared Library → Favorites → photo."""
    root = await media_source.async_browse_media(hass, f"media-source://{DOMAIN}")
    assert root.title == "iCloud Shared Photos"
    titles = [child.title for child in root.children]
    assert titles == [
        "Shared Library",
        "Personal Library",
        "Shared Albums",
        "iCloud Favorites",
    ]

    shared = await media_source.async_browse_media(
        hass, root.children[0].media_content_id
    )
    assert [c.title for c in shared.children] == ["Library", "Favorites"]

    favorites = await media_source.async_browse_media(
        hass, shared.children[1].media_content_id
    )
    assert [c.title for c in favorites.children] == ["shared-fav.heic", "dup.heic"]
    photo = favorites.children[0]
    assert SHARED_ZONE in photo.media_content_id
    assert photo.can_play
    assert photo.thumbnail.startswith("/api/icloud_shared_photos/serve/thumb/")

    # HEIC original -> "auto" serves the JPEG derivative
    resolved = await media_source.async_resolve_media(
        hass, photo.media_content_id, None
    )
    assert resolved.mime_type == "image/jpeg"
    assert resolved.url.startswith("/api/icloud_shared_photos/serve/full/")

    aioclient_mock.get("https://cdn.example/S1/med", content=b"JPEGDATA")
    aioclient_mock.get("https://cdn.example/S1/thumb", content=b"THUMB")
    client = await hass_client()
    resp = await client.get(resolved.url)
    assert resp.status == 200
    assert await resp.read() == b"JPEGDATA"
    assert resp.headers["Content-Type"] == "image/jpeg"

    resp = await client.get(photo.thumbnail)
    assert resp.status == 200
    assert await resp.read() == b"THUMB"


async def test_all_favorites(hass: HomeAssistant, setup: MockConfigEntry) -> None:
    """iCloud Favorites → All Favorites merges both libraries."""
    root = await media_source.async_browse_media(hass, f"media-source://{DOMAIN}")
    favs = await media_source.async_browse_media(
        hass, root.children[3].media_content_id
    )
    assert [c.title for c in favs.children] == [
        "Personal Favorites",
        "Shared Favorites",
        "All Favorites",
    ]
    all_favs = await media_source.async_browse_media(
        hass, favs.children[2].media_content_id
    )
    assert [c.title for c in all_favs.children] == [
        "shared-fav.heic",
        "dup.heic",
        "personal.jpg",
    ]
    # the JPEG original is served as-is
    resolved = await media_source.async_resolve_media(
        hass, all_favs.children[2].media_content_id, None
    )
    assert resolved.mime_type == "image/jpeg"


async def test_stale_url_is_refreshed(
    hass: HomeAssistant, setup: MockConfigEntry, hass_client, aioclient_mock
) -> None:
    """An expired CDN URL triggers one fresh asset lookup."""
    root = await media_source.async_browse_media(hass, f"media-source://{DOMAIN}")
    shared = await media_source.async_browse_media(
        hass, root.children[0].media_content_id
    )
    favorites = await media_source.async_browse_media(
        hass, shared.children[1].media_content_id
    )
    resolved = await media_source.async_resolve_media(
        hass, favorites.children[0].media_content_id, None
    )
    aioclient_mock.get("https://cdn.example/S1/med", status=410)
    client = await hass_client()
    resp = await client.get(resolved.url)
    # both attempts hit the (still expired) URL -> bad gateway, no crash
    assert resp.status == 502
    assert aioclient_mock.call_count == 2


async def test_bad_token(
    hass: HomeAssistant, setup: MockConfigEntry, hass_client
) -> None:
    """Garbage tokens are rejected."""
    client = await hass_client()
    resp = await client.get("/api/icloud_shared_photos/serve/full/!!!")
    assert resp.status == 400


async def test_config_flow_requires_icloud(hass: HomeAssistant) -> None:
    """The config flow aborts without a core iCloud entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "icloud_not_configured"


async def test_config_and_options_flow(hass: HomeAssistant) -> None:
    """Config flow creates the entry; options flow stores options."""
    MockConfigEntry(domain="icloud", title="me@example.com").add_to_hass(hass)
    with patch(
        "custom_components.icloud_shared_photos.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == "form"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] == "create_entry"
        entry = result["result"]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == "form"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "max_items": 100,
                "include_videos": True,
                "image_version": "original",
                "cache_ttl": 60,
            },
        )
        assert result["type"] == "create_entry"
        assert entry.options["max_items"] == 100


async def test_browse_errors(
    hass: HomeAssistant, setup: MockConfigEntry, fake_api: MagicMock
) -> None:
    """Unknown libraries and unauthenticated accounts surface as BrowseError."""
    from homeassistant.components.media_player import BrowseError

    entry_id = setup.entry_id
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(
            hass, f"media-source://{DOMAIN}/{entry_id}/lib/SharedSync-unknown"
        )
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(
            hass, f"media-source://{DOMAIN}/does-not-exist"
        )
    fake_api.requires_2fa = True
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(
            hass, f"media-source://{DOMAIN}/{entry_id}"
        )


async def test_shared_albums_browse(
    hass: HomeAssistant, setup: MockConfigEntry
) -> None:
    """Media → iCloud Shared Photos → Shared Albums → album → photos."""
    root = await media_source.async_browse_media(hass, f"media-source://{DOMAIN}")
    albums = await media_source.async_browse_media(
        hass, root.children[2].media_content_id
    )
    assert [c.title for c in albums.children] == ["Bilderrahmen"]
    album = await media_source.async_browse_media(
        hass, albums.children[0].media_content_id
    )
    assert album.title.endswith("Shared Albums / Bilderrahmen")
    assert [c.title for c in album.children] == [
        "IMG_0356.HEIC",
        "Ferien Übersicht.JPG",
    ]
    resolved = await media_source.async_resolve_media(
        hass, album.children[0].media_content_id, None
    )
    assert resolved.mime_type == "image/jpeg"


async def test_get_album_photos_service(
    hass: HomeAssistant, setup: MockConfigEntry, hass_client_no_auth, aioclient_mock
) -> None:
    """The service returns signed URLs that serve JPEG without login."""
    from homeassistant.core_config import async_process_ha_core_config
    from homeassistant.exceptions import ServiceValidationError

    await async_process_ha_core_config(
        hass, {"internal_url": "http://192.168.1.10:8123"}
    )
    response = await hass.services.async_call(
        DOMAIN,
        "get_album_photos",
        {"album": "bilderrahmen"},
        blocking=True,
        return_response=True,
    )
    assert response["album"] == "Bilderrahmen"
    assert response["account"] == "me@example.com"
    assert response["count"] == 2
    first, second = response["photos"]
    assert first["filename"] == "IMG_0356.HEIC"
    assert first["device_filename"].startswith("IMG_0356_")
    assert first["device_filename"].endswith(".jpg")
    assert second["device_filename"].startswith("Ferien_Ubersicht_")
    assert first["date"].startswith("2024-07-03")
    assert first["url"].startswith(
        "http://192.168.1.10:8123/api/icloud_shared_photos/serve/full/"
    )
    assert "authSig=" in first["url"]

    aioclient_mock.get("https://cdn.example/A1/med", content=b"JPEG-A1")
    client = await hass_client_no_auth()
    resp = await client.get(first["url"].removeprefix("http://192.168.1.10:8123"))
    assert resp.status == 200
    assert await resp.read() == b"JPEG-A1"
    assert resp.headers["Content-Type"] == "image/jpeg"
    # without signature the view requires authentication
    unsigned = first["url"].split("?")[0].removeprefix("http://192.168.1.10:8123")
    assert (await client.get(unsigned)).status == 401

    with pytest.raises(ServiceValidationError, match="available: Bilderrahmen"):
        await hass.services.async_call(
            DOMAIN,
            "get_album_photos",
            {"album": "Ferien"},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(ServiceValidationError, match="No loaded Apple iCloud"):
        await hass.services.async_call(
            DOMAIN,
            "get_album_photos",
            {"album": "Bilderrahmen", "account": "other@example.com"},
            blocking=True,
            return_response=True,
        )
