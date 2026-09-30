"""Services of the iCloud Shared Photos integration.

``get_album_photos`` lists the photos of a Shared Album together with signed,
time-limited URLs. The URLs are served by this integration's streaming view,
so HEIC photos arrive as JPEG (with the default image version option). This
lets automations push the photos to displays such as a BLOOMIN8 frame.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import re
from typing import Any
import unicodedata

import voluptuous as vol

from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    ATTR_ACCOUNT,
    ATTR_ALBUM,
    ATTR_EXPIRES,
    CONF_CACHE_TTL,
    CONF_INCLUDE_VIDEOS,
    CONF_MAX_ITEMS,
    DEFAULT_URL_EXPIRES,
    DOMAIN,
    SERVE_URL,
    SERVICE_GET_ALBUM_PHOTOS,
    SERVICE_INSPECT_ZONES,
)
from .identifier import VIEW_LIBRARY, PhotosIdentifier
from .library import (
    PhotoNotFoundError,
    PhotoRef,
    PhotosUnavailableError,
    SharedAlbumInfo,
    asset_date,
)
from .media_source import (
    _account_cache,
    _encode_token,
    _icloud_api,
    _icloud_entries,
    _options,
    _own_entry,
)

GET_ALBUM_PHOTOS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ALBUM): cv.string,
        vol.Optional(ATTR_ACCOUNT): cv.string,
        vol.Optional(ATTR_EXPIRES, default=DEFAULT_URL_EXPIRES): vol.All(
            vol.Coerce(int), vol.Range(min=60, max=7 * 86400)
        ),
    }
)


INSPECT_ZONES_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ACCOUNT): cv.string,
        vol.Optional("limit", default=200): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=500)
        ),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ALBUM_PHOTOS,
        _async_get_album_photos,
        schema=GET_ALBUM_PHOTOS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_INSPECT_ZONES,
        _async_inspect_zones,
        schema=INSPECT_ZONES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def device_filename(ref: PhotoRef) -> str:
    """Return a stable, device-safe JPEG filename for a photo.

    The name keeps the original stem for readability and adds a hash of the
    asset id, so two photos called ``IMG_0001.HEIC`` never collide.
    """
    stem = str(getattr(ref.photo, "filename", None) or "photo").rsplit(".", 1)[0]
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:40] or "photo"
    digest = hashlib.sha1(f"{ref.zone}/{ref.photo_id}".encode()).hexdigest()[:8]
    return f"{stem}_{digest}.jpg"


def _select_entries(hass: HomeAssistant, account: str | None) -> list[ConfigEntry]:
    entries = _icloud_entries(hass)
    if account is not None:
        wanted = account.strip().casefold()
        entries = [
            entry
            for entry in entries
            if entry.entry_id == account or entry.title.strip().casefold() == wanted
        ]
    if not entries:
        raise ServiceValidationError(
            f"No loaded Apple iCloud account{f' {account!r}' if account else ''} found"
        )
    return entries


async def _async_get_album_photos(call: ServiceCall) -> ServiceResponse:
    """List a Shared Album with signed photo URLs."""
    hass = call.hass
    if _own_entry(hass) is None:
        raise ServiceValidationError(
            "The iCloud Shared Photos integration is not loaded"
        )
    album_name: str = call.data[ATTR_ALBUM]
    expires = timedelta(seconds=call.data[ATTR_EXPIRES])
    options = _options(hass)

    try:
        base_url = get_url(hass, prefer_external=False)
    except NoURLAvailableError as err:
        raise HomeAssistantError(
            "No Home Assistant URL is configured (Settings → System → Network)"
        ) from err

    not_found: list[str] = []
    for entry in _select_entries(hass, call.data.get(ATTR_ACCOUNT)):
        account = _account_cache(hass, entry)
        api = _icloud_api(entry)

        def _load(
            account: Any = account, api: Any = api
        ) -> tuple[SharedAlbumInfo, list[PhotoRef]]:
            info = account.find_shared_album(
                api, album_name, ttl=options[CONF_CACHE_TTL]
            )
            refs = account.list_album(
                api,
                info.zone,
                info.source_album,
                max_items=options[CONF_MAX_ITEMS],
                include_videos=options[CONF_INCLUDE_VIDEOS],
                ttl=options[CONF_CACHE_TTL],
            )
            return info, refs

        try:
            info, refs = await hass.async_add_executor_job(_load)
        except PhotoNotFoundError as err:
            not_found.append(f"{entry.title}: {err}")
            continue
        except PhotosUnavailableError as err:
            raise HomeAssistantError(str(err)) from err

        photos: list[dict[str, Any]] = []
        for ref in refs:
            identifier = PhotosIdentifier(
                entry_id=entry.entry_id,
                view=VIEW_LIBRARY,
                zone=ref.zone,
                album=ref.album,
                photo_id=ref.photo_id,
            )
            path = async_sign_path(
                hass,
                SERVE_URL.format(version="full", token=_encode_token(identifier)),
                expires,
                use_content_user=True,
            )
            date = asset_date(ref.photo)
            photos.append(
                {
                    "id": ref.photo_id,
                    "filename": getattr(ref.photo, "filename", None) or ref.photo_id,
                    "device_filename": device_filename(ref),
                    "date": date.isoformat() if date.timestamp() > 0 else None,
                    "media_content_id": f"media-source://{DOMAIN}/{identifier}",
                    "url": f"{base_url}{path}",
                }
            )
        return {
            "account": entry.title,
            "album": info.title,
            "album_id": info.album_id,
            "count": len(photos),
            "photos": photos,
        }

    raise ServiceValidationError("; ".join(not_found))


async def _async_inspect_zones(call: ServiceCall) -> ServiceResponse:
    """Describe the iCloud photo zones (diagnostics for Shared Albums)."""
    hass = call.hass
    if _own_entry(hass) is None:
        raise ServiceValidationError(
            "The iCloud Shared Photos integration is not loaded"
        )
    accounts: dict[str, Any] = {}
    for entry in _select_entries(hass, call.data.get(ATTR_ACCOUNT)):
        account = _account_cache(hass, entry)
        api = _icloud_api(entry)
        try:
            accounts[entry.title] = await hass.async_add_executor_job(
                lambda account=account, api=api: account.inspect_zones(
                    api, limit=call.data["limit"]
                )
            )
        except PhotosUnavailableError as err:
            accounts[entry.title] = {"error": str(err)}
    return {"accounts": accounts}
