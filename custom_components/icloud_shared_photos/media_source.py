"""Media source exposing the iCloud Shared Photo Library and favorites."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
import binascii
import logging
from typing import TYPE_CHECKING, Any, override
import urllib.parse

from aiohttp import ClientError, ClientTimeout, hdrs, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.static import CACHE_HEADERS
from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ALBUM_FAVORITES,
    ALBUM_LIBRARY,
    CONF_CACHE_TTL,
    CONF_IMAGE_VERSION,
    CONF_INCLUDE_VIDEOS,
    CONF_MAX_ITEMS,
    DEFAULT_CACHE_TTL,
    DEFAULT_IMAGE_VERSION,
    DEFAULT_INCLUDE_VIDEOS,
    DEFAULT_MAX_ITEMS,
    DOMAIN,
    FAVORITES_ALL,
    FAVORITES_PERSONAL,
    FAVORITES_SHARED,
    ICLOUD_DOMAIN,
    IMAGE_VERSION_MEDIUM,
    IMAGE_VERSION_ORIGINAL,
    MEDIA_SOURCE_TITLE,
    PRIMARY_ZONE_NAME,
    SERVE_URL,
)
from .identifier import (
    VIEW_FAVORITES,
    VIEW_LIBRARY,
    InvalidIdentifier,
    PhotosIdentifier,
)
from .library import (
    AccountPhotos,
    PhotoNotFoundError,
    PhotoRef,
    PhotosUnavailableError,
    item_type,
)

if TYPE_CHECKING:
    from . import IcloudSharedPhotosConfigEntry

_LOGGER = logging.getLogger(__package__)

# CloudKit UTI -> MIME type
_UTI_MIME_TYPES = {
    "public.jpeg": "image/jpeg",
    "public.png": "image/png",
    "public.heic": "image/heic",
    "public.heif": "image/heif",
    "com.apple.quicktime-movie": "video/quicktime",
    "public.mpeg-4": "video/mp4",
    "com.apple.m4v-video": "video/mp4",
}
_EXT_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
}
# Formats every browser and most displays can render directly.
_WEB_SAFE_IMAGE_TYPES = {"public.jpeg", "public.png"}

_FAVORITES_TITLES = {
    FAVORITES_PERSONAL: "Personal Favorites",
    FAVORITES_SHARED: "Shared Favorites",
    FAVORITES_ALL: "All Favorites",
}

# CDN status codes that indicate an expired / invalid signed download URL.
_STALE_URL_STATUSES = {401, 403, 404, 410}


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up the iCloud Shared Photos media source."""
    return IcloudSharedPhotosMediaSource(hass)


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register the HTTP view that streams photos from iCloud."""
    hass.http.register_view(IcloudSharedPhotosView(hass))


# --------------------------------------------------------------------- helpers


def _own_entry(hass: HomeAssistant) -> IcloudSharedPhotosConfigEntry | None:
    """Return this integration's loaded config entry, if any."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


def _options(hass: HomeAssistant) -> dict[str, Any]:
    entry = _own_entry(hass)
    options = dict(entry.options) if entry is not None else {}
    return {
        CONF_MAX_ITEMS: int(options.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS)),
        CONF_INCLUDE_VIDEOS: bool(
            options.get(CONF_INCLUDE_VIDEOS, DEFAULT_INCLUDE_VIDEOS)
        ),
        CONF_IMAGE_VERSION: str(options.get(CONF_IMAGE_VERSION, DEFAULT_IMAGE_VERSION)),
        CONF_CACHE_TTL: float(options.get(CONF_CACHE_TTL, DEFAULT_CACHE_TTL)),
    }


def _account_cache(hass: HomeAssistant, icloud_entry: ConfigEntry) -> AccountPhotos:
    """Return the per-account RAM cache (kept on our own config entry)."""
    own = _own_entry(hass)
    if own is None:
        raise Unresolvable("The iCloud Shared Photos integration is not loaded")
    accounts: dict[str, AccountPhotos] = own.runtime_data.accounts
    if (account := accounts.get(icloud_entry.entry_id)) is None:
        account = accounts[icloud_entry.entry_id] = AccountPhotos(icloud_entry.title)
    return account


def _icloud_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Return the loaded config entries of the core iCloud integration."""
    return hass.config_entries.async_loaded_entries(ICLOUD_DOMAIN)


def _icloud_entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != ICLOUD_DOMAIN:
        raise Unresolvable(f"iCloud account '{entry_id}' not found")
    if entry.state is not ConfigEntryState.LOADED:
        raise Unresolvable(f"iCloud account '{entry.title}' is not loaded")
    return entry


def _icloud_api(entry: ConfigEntry) -> Any:
    """Return the authenticated PyiCloudService of a core iCloud entry.

    The core integration stores an ``IcloudAccount`` in ``entry.runtime_data``
    whose ``api`` attribute is the ``PyiCloudService`` (``None`` while the
    account is not authenticated). This is not a public contract of the core
    integration, therefore it is accessed defensively.
    """
    account = getattr(entry, "runtime_data", None)
    if account is None:
        raise Unresolvable(f"iCloud account '{entry.title}' is not initialized")
    return getattr(account, "api", None)


def _encode_token(identifier: PhotosIdentifier) -> str:
    return urlsafe_b64encode(str(identifier).encode()).decode()


def _decode_token(token: str) -> PhotosIdentifier:
    return PhotosIdentifier.parse(urlsafe_b64decode(token.encode()).decode())


def _pick_version(photo: Any, preference: str, *, thumbnail: bool) -> str | None:
    """Return the pyicloud resource key that should be served."""
    try:
        resources: dict[str, Any] = photo.resources
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Asset %s has no readable resources", getattr(photo, "id", "?"))
        return None
    is_video = item_type(photo) == "movie"

    if thumbnail:
        order = (
            ["thumb_image", "medium_image"]
            if is_video
            else ["thumb", "medium", "original"]
        )
    elif is_video:
        order = ["original", "medium"]
    elif preference == IMAGE_VERSION_ORIGINAL:
        order = ["original", "medium"]
    elif preference == IMAGE_VERSION_MEDIUM:
        order = ["medium", "original"]
    else:  # auto: original if web-safe (JPEG/PNG), else the JPEG derivative
        original = resources.get("original")
        if (
            original is not None
            and getattr(original, "type", None) in _WEB_SAFE_IMAGE_TYPES
        ):
            order = ["original", "medium"]
        else:
            order = ["medium", "original"]

    for key in order:
        resource = resources.get(key)
        if resource is not None and getattr(resource, "url", None):
            return key
    return None


def _mime_type(photo: Any, version: str | None) -> str:
    resource = photo.resources.get(version) if version else None
    if resource is not None:
        if (
            mime := _UTI_MIME_TYPES.get(getattr(resource, "type", None) or "")
        ) is not None:
            return mime
        filename = getattr(resource, "filename", "") or ""
    else:
        filename = getattr(photo, "filename", "") or ""
    for ext, mime in _EXT_MIME_TYPES.items():
        if filename.lower().endswith(ext):
            return mime
    return "video/mp4" if item_type(photo) == "movie" else "image/jpeg"


# ---------------------------------------------------------------- media source


class IcloudSharedPhotosMediaSource(MediaSource):
    """Media source for the iCloud Shared Photo Library and favorites."""

    name = MEDIA_SOURCE_TITLE

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def _run(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run blocking pyicloud work in the executor and map errors."""
        try:
            return await self.hass.async_add_executor_job(lambda: func(*args, **kwargs))
        except (PhotosUnavailableError, PhotoNotFoundError) as err:
            raise BrowseError(str(err)) from err

    # ------------------------------------------------------------- resolve

    @override
    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a photo to a URL served by this integration."""
        try:
            identifier = PhotosIdentifier.parse(item.identifier)
        except InvalidIdentifier as err:
            raise Unresolvable(str(err)) from err
        if not identifier.is_photo:
            raise Unresolvable("Only single photos can be resolved")

        entry = _icloud_entry(self.hass, identifier.entry_id)
        account = _account_cache(self.hass, entry)
        api = _icloud_api(entry)
        assert identifier.zone and identifier.album and identifier.photo_id
        try:
            photo = await self._run(
                account.get_photo,
                api,
                identifier.zone,
                identifier.album,
                identifier.photo_id,
            )
        except BrowseError as err:
            raise Unresolvable(str(err)) from err

        version = _pick_version(
            photo, _options(self.hass)[CONF_IMAGE_VERSION], thumbnail=False
        )
        if version is None:
            _LOGGER.warning(
                "Asset %s in %s has no downloadable resource",
                identifier.photo_id,
                identifier.zone,
            )
            raise Unresolvable("Photo has no downloadable resource")
        _LOGGER.debug(
            "Resolved %s/%s/%s (%s) to version '%s'",
            identifier.zone,
            identifier.album,
            identifier.photo_id,
            getattr(photo, "filename", "?"),
            version,
        )
        return PlayMedia(
            SERVE_URL.format(version="full", token=_encode_token(identifier)),
            _mime_type(photo, version),
        )

    # -------------------------------------------------------------- browse

    @override
    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse the media source."""
        try:
            return await self._async_browse(item)
        except Unresolvable as err:
            # Home Assistant's browse API only reports BrowseError to clients.
            raise BrowseError(str(err)) from err

    async def _async_browse(self, item: MediaSourceItem) -> BrowseMediaSource:
        if _own_entry(self.hass) is None:
            raise BrowseError("The iCloud Shared Photos integration is not loaded")

        if not item.identifier:
            entries = _icloud_entries(self.hass)
            if len(entries) == 1:
                return await self._browse_account(
                    PhotosIdentifier(entry_id=entries[0].entry_id), root=True
                )
            return self._browse_accounts(entries)

        try:
            identifier = PhotosIdentifier.parse(item.identifier)
        except InvalidIdentifier as err:
            raise BrowseError(str(err)) from err

        if identifier.view is None:
            return await self._browse_account(identifier)
        if identifier.view == VIEW_FAVORITES:
            if identifier.collection is None:
                return self._browse_favorites_collections(identifier)
            return await self._browse_favorites(identifier)
        if identifier.view == VIEW_LIBRARY:
            if identifier.album is None:
                return await self._browse_library(identifier)
            if identifier.photo_id is None:
                return await self._browse_album(identifier)
        raise BrowseError("Unknown media item")

    @staticmethod
    def _directory(
        identifier: PhotosIdentifier | None,
        title: str,
        children: list[BrowseMediaSource] | None = None,
        *,
        children_media_class: str = MediaClass.DIRECTORY,
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=str(identifier) if identifier is not None else None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.ALBUM,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=children_media_class,
        )

    def _browse_accounts(self, entries: list[ConfigEntry]) -> BrowseMediaSource:
        if not entries:
            _LOGGER.info(
                "No loaded core iCloud integration entry found; "
                "set up the 'Apple iCloud' integration first"
            )
        return self._directory(
            None,
            MEDIA_SOURCE_TITLE,
            [
                self._directory(PhotosIdentifier(entry_id=entry.entry_id), entry.title)
                for entry in entries
            ],
        )

    async def _browse_account(
        self, identifier: PhotosIdentifier, *, root: bool = False
    ) -> BrowseMediaSource:
        entry = _icloud_entry(self.hass, identifier.entry_id)
        account = _account_cache(self.hass, entry)
        libraries = await self._run(account.libraries, _icloud_api(entry))

        entry_id = identifier.entry_id
        shared = [info for info in libraries.values() if info.shared]
        children: list[BrowseMediaSource] = []
        for index, info in enumerate(shared, start=1):
            title = "Shared Library" if len(shared) == 1 else f"Shared Library {index}"
            children.append(
                self._directory(
                    PhotosIdentifier(
                        entry_id=entry_id, view=VIEW_LIBRARY, zone=info.zone
                    ),
                    title,
                )
            )
        if PRIMARY_ZONE_NAME in libraries:
            children.append(
                self._directory(
                    PhotosIdentifier(
                        entry_id=entry_id, view=VIEW_LIBRARY, zone=PRIMARY_ZONE_NAME
                    ),
                    "Personal Library",
                )
            )
        children.append(
            self._directory(
                PhotosIdentifier(entry_id=entry_id, view=VIEW_FAVORITES),
                "iCloud Favorites",
            )
        )
        title = MEDIA_SOURCE_TITLE if root else f"{MEDIA_SOURCE_TITLE} / {entry.title}"
        # At the root level the directory has no identifier (it *is* the root).
        return self._directory(None if root else identifier, title, children)

    async def _browse_library(self, identifier: PhotosIdentifier) -> BrowseMediaSource:
        entry = _icloud_entry(self.hass, identifier.entry_id)
        account = _account_cache(self.hass, entry)
        libraries = await self._run(account.libraries, _icloud_api(entry))
        if (info := libraries.get(identifier.zone or "")) is None:
            raise BrowseError(f"Photo library '{identifier.zone}' not found")
        library_title = "Shared Library" if info.shared else "Personal Library"
        children = [
            self._directory(
                PhotosIdentifier(
                    entry_id=identifier.entry_id,
                    view=VIEW_LIBRARY,
                    zone=identifier.zone,
                    album=album,
                ),
                album,
            )
            for album in (ALBUM_LIBRARY, ALBUM_FAVORITES)
        ]
        return self._directory(
            identifier, f"{MEDIA_SOURCE_TITLE} / {library_title}", children
        )

    def _browse_favorites_collections(
        self, identifier: PhotosIdentifier
    ) -> BrowseMediaSource:
        children = [
            self._directory(
                PhotosIdentifier(
                    entry_id=identifier.entry_id,
                    view=VIEW_FAVORITES,
                    collection=collection,
                ),
                title,
            )
            for collection, title in _FAVORITES_TITLES.items()
        ]
        return self._directory(
            identifier, f"{MEDIA_SOURCE_TITLE} / iCloud Favorites", children
        )

    async def _browse_album(self, identifier: PhotosIdentifier) -> BrowseMediaSource:
        entry = _icloud_entry(self.hass, identifier.entry_id)
        account = _account_cache(self.hass, entry)
        options = _options(self.hass)
        assert identifier.zone and identifier.album
        refs: list[PhotoRef] = await self._run(
            account.list_album,
            _icloud_api(entry),
            identifier.zone,
            identifier.album,
            max_items=options[CONF_MAX_ITEMS],
            include_videos=options[CONF_INCLUDE_VIDEOS],
            ttl=options[CONF_CACHE_TTL],
        )
        library_title = (
            "Personal Library"
            if identifier.zone == PRIMARY_ZONE_NAME
            else "Shared Library"
        )
        return self._photos_directory(
            identifier,
            f"{MEDIA_SOURCE_TITLE} / {library_title} / {identifier.album}",
            refs,
        )

    async def _browse_favorites(
        self, identifier: PhotosIdentifier
    ) -> BrowseMediaSource:
        entry = _icloud_entry(self.hass, identifier.entry_id)
        account = _account_cache(self.hass, entry)
        options = _options(self.hass)
        assert identifier.collection
        refs: list[PhotoRef] = await self._run(
            account.list_favorites,
            _icloud_api(entry),
            identifier.collection,
            max_items=options[CONF_MAX_ITEMS],
            include_videos=options[CONF_INCLUDE_VIDEOS],
            ttl=options[CONF_CACHE_TTL],
        )
        return self._photos_directory(
            identifier,
            f"{MEDIA_SOURCE_TITLE} / {_FAVORITES_TITLES[identifier.collection]}",
            refs,
        )

    def _photos_directory(
        self, identifier: PhotosIdentifier, title: str, refs: list[PhotoRef]
    ) -> BrowseMediaSource:
        children: list[BrowseMediaSource] = []
        for ref in refs:
            photo_identifier = PhotosIdentifier(
                entry_id=identifier.entry_id,
                view=VIEW_LIBRARY,
                zone=ref.zone,
                album=ref.album,
                photo_id=ref.photo_id,
            )
            is_image = item_type(ref.photo) == "image"
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=str(photo_identifier),
                    media_class=MediaClass.IMAGE if is_image else MediaClass.VIDEO,
                    media_content_type=MediaType.IMAGE if is_image else MediaType.VIDEO,
                    title=getattr(ref.photo, "filename", None) or ref.photo_id,
                    can_play=True,
                    can_expand=False,
                    thumbnail=SERVE_URL.format(
                        version="thumb", token=_encode_token(photo_identifier)
                    ),
                )
            )
        return self._directory(
            identifier, title, children, children_media_class=MediaClass.IMAGE
        )


# ------------------------------------------------------------------- HTTP view


class IcloudSharedPhotosView(HomeAssistantView):
    """Stream photos from iCloud to the client without storing them."""

    url = SERVE_URL
    name = "api:icloud_shared_photos:serve"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self.hass = hass

    async def get(
        self, request: web.Request, version: str, token: str
    ) -> web.StreamResponse:
        """Stream a photo (``full``) or its thumbnail (``thumb``)."""
        if version not in ("full", "thumb"):
            raise web.HTTPNotFound
        try:
            identifier = _decode_token(token)
        except (
            InvalidIdentifier,
            binascii.Error,
            UnicodeDecodeError,
            ValueError,
        ) as err:
            _LOGGER.debug("Invalid media token %s: %s", token, err)
            raise web.HTTPBadRequest from err
        if not identifier.is_photo:
            raise web.HTTPBadRequest
        assert identifier.zone and identifier.album and identifier.photo_id

        try:
            entry = _icloud_entry(self.hass, identifier.entry_id)
            account = _account_cache(self.hass, entry)
            api = _icloud_api(entry)
        except Unresolvable as err:
            _LOGGER.warning("Cannot serve iCloud photo: %s", err)
            raise web.HTTPNotFound from err

        thumbnail = version == "thumb"
        preference = _options(self.hass)[CONF_IMAGE_VERSION]

        for attempt in (1, 2):
            try:
                photo = await self.hass.async_add_executor_job(
                    lambda refresh=(attempt == 2): account.get_photo(
                        api,
                        identifier.zone,
                        identifier.album,
                        identifier.photo_id,
                        refresh=refresh,
                    )
                )
            except PhotoNotFoundError as err:
                raise web.HTTPNotFound from err
            except PhotosUnavailableError as err:
                _LOGGER.warning("Cannot serve iCloud photo: %s", err)
                raise web.HTTPServiceUnavailable from err

            resource_key = _pick_version(photo, preference, thumbnail=thumbnail)
            resource = photo.resources.get(resource_key) if resource_key else None
            url = getattr(resource, "url", None)
            if not url:
                _LOGGER.warning(
                    "No download URL for asset %s (%s) in %s, version %s",
                    identifier.photo_id,
                    getattr(photo, "filename", "?"),
                    identifier.zone,
                    resource_key or version,
                )
                raise web.HTTPNotFound

            headers = {}
            if hdrs.RANGE in request.headers:
                headers[hdrs.RANGE] = request.headers[hdrs.RANGE]
            try:
                upstream = await async_get_clientsession(self.hass).get(
                    url,
                    headers=headers,
                    timeout=ClientTimeout(
                        connect=15, sock_connect=15, sock_read=30, total=None
                    ),
                )
            except (ClientError, TimeoutError) as err:
                # Never log the URL: it is a signed, credential-like link.
                _LOGGER.warning(
                    "Fetching asset %s from iCloud failed: %s",
                    identifier.photo_id,
                    type(err).__name__,
                )
                raise web.HTTPBadGateway from err

            if upstream.status in _STALE_URL_STATUSES and attempt == 1:
                _LOGGER.debug(
                    "iCloud returned HTTP %s for asset %s, refreshing download URL",
                    upstream.status,
                    identifier.photo_id,
                )
                upstream.release()
                continue
            break

        if upstream.status >= 400:
            _LOGGER.warning(
                "iCloud returned HTTP %s for asset %s (%s)",
                upstream.status,
                identifier.photo_id,
                resource_key,
            )
            upstream.release()
            raise web.HTTPBadGateway

        filename = getattr(resource, "filename", None) or getattr(
            photo, "filename", identifier.photo_id
        )
        response_headers: dict[str, str] = dict(CACHE_HEADERS)
        response_headers[hdrs.CONTENT_TYPE] = _mime_type(photo, resource_key)
        response_headers[hdrs.CONTENT_DISPOSITION] = (
            f"inline; filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"
        )
        for header in (
            hdrs.CONTENT_LENGTH,
            hdrs.LAST_MODIFIED,
            hdrs.ACCEPT_RANGES,
            hdrs.CONTENT_RANGE,
        ):
            if header in upstream.headers:
                response_headers[header] = upstream.headers[header]

        response = web.StreamResponse(status=upstream.status, headers=response_headers)
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(65536):
                await response.write(chunk)
        except (ClientError, TimeoutError) as err:
            _LOGGER.warning(
                "Streaming asset %s from iCloud aborted: %s",
                identifier.photo_id,
                type(err).__name__,
            )
        finally:
            upstream.release()
        await response.write_eof()
        return response
