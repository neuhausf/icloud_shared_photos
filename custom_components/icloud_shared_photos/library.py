"""Thin, synchronous access layer on top of pyicloud's Photos service.

Everything in here performs blocking network I/O through pyicloud and must be
run in an executor thread. The module intentionally has no Home Assistant
imports so it can be unit tested with fake pyicloud objects.

All iCloud logic (zone discovery, smart album queries, CloudKit paging) is
delegated to pyicloud (``PhotosService.libraries`` / ``PhotoLibrary.albums`` /
``PhotoAlbum.photos``). This module only adds:

* selection of the Shared Library (``SharedSync-*``) zones,
* access to legacy Shared Albums (photo streams) under the pseudo zone
  ``SharedAlbums``, with the album GUID as album name,
* in-RAM caching of album listings and resolved assets,
* merging of personal and shared favorites without duplicates.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

from .const import (
    ALBUM_FAVORITES,
    ASSET_URL_TTL,
    FAVORITES_ALL,
    FAVORITES_PERSONAL,
    FAVORITES_SHARED,
    MAX_ASSET_CACHE_SIZE,
    PRIMARY_ZONE_NAME,
    SHARED_ALBUMS_ZONE,
    SHARED_LIBRARY_ZONE_PREFIX,
    SUPPORTED_ALBUMS,
)

try:  # pragma: no cover - import guard for unit tests without pyicloud
    from pyicloud.exceptions import PyiCloudException
except ImportError:  # pragma: no cover

    class PyiCloudException(Exception):  # type: ignore[no-redef]
        """Fallback when pyicloud is not installed."""


try:  # pragma: no cover - module location is a pyicloud implementation detail
    from pyicloud.common.cloudkit.client import CloudKitApiError
except ImportError:  # pragma: no cover

    class CloudKitApiError(Exception):  # type: ignore[no-redef]
        """Fallback when the CloudKit client is not importable."""


try:  # pragma: no cover
    from requests import RequestException
except ImportError:  # pragma: no cover

    class RequestException(Exception):  # type: ignore[no-redef]
        """Fallback when requests is not installed."""


_LOGGER = logging.getLogger(__package__)

# Errors pyicloud may raise while talking to iCloud.
ICLOUD_ERRORS: tuple[type[BaseException], ...] = (
    PyiCloudException,
    CloudKitApiError,
    RequestException,
)

_EPOCH = datetime.fromtimestamp(0, UTC)


class PhotosUnavailableError(Exception):
    """iCloud Photos cannot be accessed for this account right now."""


class PhotoNotFoundError(Exception):
    """A requested library, album or photo does not exist."""


@dataclass(frozen=True, slots=True)
class LibraryInfo:
    """A photo library (CloudKit zone) of an account."""

    zone: str
    shared: bool
    library: Any = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class SharedAlbumInfo:
    """A legacy Shared Album (photo stream)."""

    album_id: str
    title: str
    album: Any = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class PhotoRef:
    """A photo together with the library/album it was listed from."""

    zone: str
    album: str
    photo: Any = field(compare=False, repr=False)

    @property
    def photo_id(self) -> str:
        """Return the pyicloud asset id."""
        return str(self.photo.id)


def is_shared_library_zone(zone_name: str | None) -> bool:
    """Return True for a Shared Photo Library zone (``SharedSync-<UUID>``)."""
    return bool(zone_name) and str(zone_name).startswith(SHARED_LIBRARY_ZONE_PREFIX)


def photo_resources(photo: Any) -> dict[str, Any]:
    """Return the downloadable resources of an asset, keyed by version.

    CloudKit assets expose ``resources`` (objects with ``url``/``type``/
    ``filename``/``checksum``). Shared Album assets come from pyicloud's legacy
    implementation, which only has ``versions`` dicts; those are adapted to the
    same attribute interface.
    """
    try:
        return dict(photo.resources)
    except AttributeError:
        pass
    versions: dict[str, dict[str, Any]] = photo.versions
    return {
        key: SimpleNamespace(
            url=value.get("url"),
            type=value.get("type"),
            filename=value.get("filename"),
            checksum=None,
        )
        for key, value in versions.items()
    }


def asset_date(photo: Any) -> datetime:
    """Return the capture date of a pyicloud asset, never raising."""
    try:
        value = photo.asset_date
    except Exception:  # noqa: BLE001 - defensive against odd records
        return _EPOCH
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return _EPOCH


def asset_fingerprint(photo: Any) -> str | None:
    """Return the fingerprint (checksum) of the original resource, if known.

    The fingerprint is content based, so the same picture has the same value in
    the personal and in the shared library. It is used to de-duplicate
    "All Favorites".
    """
    try:
        resource = photo_resources(photo).get("original")
    except Exception:  # noqa: BLE001
        return None
    checksum = getattr(resource, "checksum", None) if resource is not None else None
    return str(checksum) if checksum else None


def item_type(photo: Any) -> str:
    """Return ``image`` or ``movie`` for a pyicloud asset, never raising."""
    try:
        return str(photo.item_type)
    except Exception:  # noqa: BLE001
        return "unknown"


class AccountPhotos:
    """Photos access + RAM cache for one authenticated iCloud account."""

    def __init__(self, name: str) -> None:
        """Initialize.

        ``name`` is only used for log messages (it is the config entry title,
        i.e. the Apple ID as shown in Home Assistant, never a secret).
        """
        self._name = name
        self._lock = threading.RLock()
        self._service: Any = None
        self._libraries: dict[str, LibraryInfo] | None = None
        self._shared_albums: tuple[float, dict[str, SharedAlbumInfo]] | None = None
        self._listings: dict[tuple[Any, ...], tuple[float, list[PhotoRef]]] = {}
        self._assets: OrderedDict[tuple[str, str], tuple[float, Any]] = OrderedDict()

    # ------------------------------------------------------------------ setup

    def _photos_service(self, api: Any) -> Any:
        """Return pyicloud's PhotosService of the shared, authenticated session."""
        if api is None:
            raise PhotosUnavailableError(
                f"iCloud account '{self._name}' is not authenticated in the core "
                "iCloud integration"
            )
        try:
            if api.requires_2fa or api.requires_2sa:
                _LOGGER.warning(
                    "iCloud account '%s' requires two-factor re-authentication; "
                    "re-authenticate it in the core iCloud integration",
                    self._name,
                )
                raise PhotosUnavailableError(
                    f"iCloud account '{self._name}' requires re-authentication"
                )
            service = api.photos
        except ICLOUD_ERRORS as err:
            _LOGGER.error(
                "Cannot access iCloud Photos for '%s': %s (%s)",
                self._name,
                type(err).__name__,
                err,
            )
            raise PhotosUnavailableError(
                f"iCloud Photos is not available for '{self._name}': {err}"
            ) from err
        if service is None:
            raise PhotosUnavailableError(
                f"iCloud Photos is not available for '{self._name}'"
            )
        with self._lock:
            if service is not self._service:
                if self._service is not None:
                    _LOGGER.debug(
                        "pyicloud PhotosService for '%s' was recreated, "
                        "dropping caches",
                        self._name,
                    )
                self._service = service
                self._libraries = None
                self._shared_albums = None
                self._listings.clear()
                self._assets.clear()
        return service

    def libraries(self, api: Any, *, refresh: bool = False) -> dict[str, LibraryInfo]:
        """Return the personal and Shared Library zones, keyed by zone name."""
        service = self._photos_service(api)
        with self._lock:
            if self._libraries is not None and not refresh:
                return self._libraries

        result: dict[str, LibraryInfo] = {}
        try:
            discovered: dict[str, Any] = dict(service.libraries)
        except ICLOUD_ERRORS as err:
            # pyicloud builds all libraries at once; a single zone that is not
            # indexed yet makes the whole discovery fail. Fall back to the
            # personal library so that at least that keeps working.
            _LOGGER.warning(
                "Discovery of iCloud photo libraries for '%s' failed (%s: %s); "
                "only the personal library is available",
                self._name,
                type(err).__name__,
                err,
            )
            discovered = {}
            try:
                discovered["root"] = service._root_library  # noqa: SLF001
            except AttributeError:
                pass

        for key, library in discovered.items():
            zone_id = getattr(library, "zone_id", None)
            zone = zone_id.get("zoneName") if isinstance(zone_id, dict) else None
            scope = getattr(library, "scope", None)
            if key == "root" or zone == PRIMARY_ZONE_NAME:
                result[PRIMARY_ZONE_NAME] = LibraryInfo(
                    zone=PRIMARY_ZONE_NAME, shared=False, library=library
                )
                _LOGGER.debug("Library '%s': personal library (PrimarySync)", key)
            elif scope == "shared-library" and is_shared_library_zone(zone):
                assert zone is not None
                result[zone] = LibraryInfo(zone=zone, shared=True, library=library)
                _LOGGER.info(
                    "Detected iCloud Shared Photo Library zone %s for '%s'",
                    zone,
                    self._name,
                )
            else:
                # "shared" = legacy Shared Albums (photo streams) and other
                # private/shared zones that are not a Shared Photo Library.
                _LOGGER.debug(
                    "Ignoring iCloud photo library '%s' (zone=%s, scope=%s)",
                    key,
                    zone,
                    scope,
                )

        _LOGGER.info(
            "iCloud photo libraries for '%s': %s",
            self._name,
            ", ".join(
                f"{info.zone} ({'shared' if info.shared else 'personal'})"
                for info in result.values()
            )
            or "none",
        )
        if not any(info.shared for info in result.values()):
            _LOGGER.info(
                "No iCloud Shared Photo Library (SharedSync-*) found for '%s'",
                self._name,
            )
        with self._lock:
            self._libraries = result
        return result

    def shared_zones(self, api: Any) -> list[str]:
        """Return the zone names of all Shared Photo Libraries."""
        return [info.zone for info in self.libraries(api).values() if info.shared]

    def shared_albums(self, api: Any, *, ttl: float) -> dict[str, SharedAlbumInfo]:
        """Return the legacy Shared Albums (photo streams), keyed by album id."""
        service = self._photos_service(api)
        now = time.monotonic()
        with self._lock:
            cached = self._shared_albums
            if cached is not None and now - cached[0] < ttl:
                return cached[1]
        # pyicloud caches the shared album list (and each album's change tag)
        # for the lifetime of the PhotosService, so albums shared after Home
        # Assistant started would never show up. Drop that cache on refresh.
        shared_library = getattr(service, "_shared_library", None)
        if shared_library is not None and hasattr(shared_library, "_albums"):
            shared_library._albums = None
        try:
            result = {
                str(album.id): SharedAlbumInfo(
                    album_id=str(album.id), title=str(album.title), album=album
                )
                for album in service.shared_streams
            }
        except ICLOUD_ERRORS as err:
            _LOGGER.error(
                "Listing shared albums for '%s' failed: %s (%s)",
                self._name,
                type(err).__name__,
                err,
            )
            raise PhotosUnavailableError(f"Cannot list shared albums: {err}") from err
        _LOGGER.debug("Found %d shared album(s) for '%s'", len(result), self._name)
        with self._lock:
            self._shared_albums = (time.monotonic(), result)
        return result

    def find_shared_album(
        self, api: Any, name_or_id: str, *, ttl: float
    ) -> SharedAlbumInfo:
        """Return a Shared Album by id or (case-insensitive) title."""
        wanted = name_or_id.strip().casefold()
        for refresh_ttl in (ttl, 0):
            albums = self.shared_albums(api, ttl=refresh_ttl)
            if (info := albums.get(name_or_id)) is not None:
                return info
            for info in albums.values():
                if info.title.strip().casefold() == wanted:
                    return info
        raise PhotoNotFoundError(
            f"Shared album '{name_or_id}' not found; available: "
            + (", ".join(sorted(info.title for info in albums.values())) or "none")
        )

    def _album(self, api: Any, zone: str, album_name: str) -> Any:
        if zone == SHARED_ALBUMS_ZONE:
            albums = self.shared_albums(api, ttl=ASSET_URL_TTL)
            if (info := albums.get(album_name)) is None:
                # The cached list may predate a newly shared album.
                albums = self.shared_albums(api, ttl=0)
                info = albums.get(album_name)
            if info is None:
                raise PhotoNotFoundError(f"Shared album '{album_name}' not found")
            return info.album
        if album_name not in SUPPORTED_ALBUMS:
            raise PhotoNotFoundError(f"Unsupported album '{album_name}'")
        info = self.libraries(api).get(zone)
        if info is None:
            raise PhotoNotFoundError(f"Photo library '{zone}' not found")
        try:
            albums = info.library.albums
            album = albums.get(album_name) or albums.find(album_name)
        except ICLOUD_ERRORS as err:
            raise PhotosUnavailableError(
                f"Cannot load albums of library '{zone}': {err}"
            ) from err
        if album is None:
            raise PhotoNotFoundError(f"Album '{album_name}' not found in '{zone}'")
        return album

    # --------------------------------------------------------------- listing

    def album_count(self, api: Any, zone: str, album_name: str) -> int | None:
        """Return the number of items in an album (one count query)."""
        album = self._album(api, zone, album_name)
        try:
            return len(album)
        except ICLOUD_ERRORS as err:
            _LOGGER.debug("Counting %s/%s failed: %s", zone, album_name, err)
            return None
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.debug("Counting %s/%s failed: %s", zone, album_name, err)
            return None

    def list_album(
        self,
        api: Any,
        zone: str,
        album_name: str,
        *,
        max_items: int,
        include_videos: bool,
        ttl: float,
    ) -> list[PhotoRef]:
        """Return up to ``max_items`` photos of an album (cached for ``ttl``)."""
        key = (zone, album_name, max_items, include_videos)
        now = time.monotonic()
        with self._lock:
            cached = self._listings.get(key)
            if cached is not None and now - cached[0] < ttl:
                return cached[1]

        album = self._album(api, zone, album_name)
        refs: list[PhotoRef] = []
        skipped_videos = 0
        started = time.monotonic()
        try:
            # album.photos pages lazily through CloudKit, islice stops paging
            # as soon as enough items were collected.
            for photo in islice(album.photos, max_items * (1 if include_videos else 4)):
                if not include_videos and item_type(photo) != "image":
                    skipped_videos += 1
                    continue
                refs.append(PhotoRef(zone=zone, album=album_name, photo=photo))
                if len(refs) >= max_items:
                    break
        except ICLOUD_ERRORS as err:
            _LOGGER.error(
                "Listing %s/%s for '%s' failed: %s (%s)",
                zone,
                album_name,
                self._name,
                type(err).__name__,
                err,
            )
            raise PhotosUnavailableError(
                f"Cannot list album '{album_name}' of '{zone}': {err}"
            ) from err

        if album_name == ALBUM_FAVORITES:
            # pyicloud returns personal favorites oldest-first and shared
            # favorites newest-first; present both newest-first.
            refs.sort(key=lambda ref: asset_date(ref.photo), reverse=True)

        _LOGGER.log(
            logging.INFO if album_name == ALBUM_FAVORITES else logging.DEBUG,
            "Loaded %d item(s) from %s %s/%s for '%s' in %.1fs "
            "(%d video(s) skipped, limit %d)",
            len(refs),
            _zone_label(zone),
            zone,
            album_name,
            self._name,
            time.monotonic() - started,
            skipped_videos,
            max_items,
        )
        now = time.monotonic()
        with self._lock:
            self._listings[key] = (now, refs)
            for ref in refs:
                self._remember_asset(zone, ref.photo, now)
        return refs

    def list_favorites(
        self,
        api: Any,
        collection: str,
        *,
        max_items: int,
        include_videos: bool,
        ttl: float,
    ) -> list[PhotoRef]:
        """Return personal, shared or all (merged, de-duplicated) favorites."""
        zones: list[str] = []
        if collection in (FAVORITES_PERSONAL, FAVORITES_ALL):
            if PRIMARY_ZONE_NAME in self.libraries(api):
                zones.append(PRIMARY_ZONE_NAME)
        if collection in (FAVORITES_SHARED, FAVORITES_ALL):
            zones.extend(self.shared_zones(api))

        lists = [
            self.list_album(
                api,
                zone,
                ALBUM_FAVORITES,
                max_items=max_items,
                include_videos=include_videos,
                ttl=ttl,
            )
            for zone in zones
        ]
        merged = merge_favorites(lists)
        if collection == FAVORITES_ALL:
            total = sum(len(refs) for refs in lists)
            _LOGGER.debug(
                "All Favorites for '%s': %d item(s), %d duplicate(s) removed",
                self._name,
                len(merged),
                total - len(merged),
            )
        return merged[:max_items]

    # ---------------------------------------------------------------- assets

    def _remember_asset(self, zone: str, photo: Any, now: float) -> None:
        key = (zone, str(photo.id))
        self._assets[key] = (now, photo)
        self._assets.move_to_end(key)
        while len(self._assets) > MAX_ASSET_CACHE_SIZE:
            self._assets.popitem(last=False)

    def get_photo(
        self,
        api: Any,
        zone: str,
        album_name: str,
        photo_id: str,
        *,
        refresh: bool = False,
    ) -> Any:
        """Return the pyicloud PhotoAsset for a photo id.

        A cached asset is only reused while its signed download URLs are
        expected to be valid; otherwise the asset is looked up again in its
        album (pyicloud performs a direct record-name query).
        """
        # Validate the api/session first so a recreated service drops caches.
        self._photos_service(api)
        key = (zone, photo_id)
        now = time.monotonic()
        with self._lock:
            cached = self._assets.get(key)
            if cached is not None and not refresh and now - cached[0] < ASSET_URL_TTL:
                self._assets.move_to_end(key)
                return cached[1]

        album = self._album(api, zone, album_name)
        try:
            photo = album.get(photo_id)
        except ICLOUD_ERRORS as err:
            _LOGGER.error(
                "Looking up asset %s in %s/%s failed: %s (%s)",
                photo_id,
                zone,
                album_name,
                type(err).__name__,
                err,
            )
            raise PhotosUnavailableError(f"Cannot look up photo: {err}") from err
        if photo is None:
            _LOGGER.warning(
                "Asset %s not found in %s/%s (deleted or no longer a favorite?)",
                photo_id,
                zone,
                album_name,
            )
            raise PhotoNotFoundError(f"Photo '{photo_id}' not found")
        with self._lock:
            self._remember_asset(zone, photo, time.monotonic())
        return photo

    def invalidate(self) -> None:
        """Drop all cached listings and assets."""
        with self._lock:
            self._libraries = None
            self._shared_albums = None
            self._listings.clear()
            self._assets.clear()


def merge_favorites(lists: Iterable[list[PhotoRef]]) -> list[PhotoRef]:
    """Merge favorites lists newest-first, dropping duplicates.

    Two entries are duplicates if they are the same asset in the same zone or
    if their original resources share the same content fingerprint. On a tie
    the Shared Library copy wins.
    """
    seen_ids: set[tuple[str, str]] = set()
    seen_fingerprints: set[str] = set()
    merged: list[PhotoRef] = []
    for ref in sorted(
        (ref for refs in lists for ref in refs),
        key=lambda ref: (asset_date(ref.photo), is_shared_library_zone(ref.zone)),
        reverse=True,
    ):
        id_key = (ref.zone, ref.photo_id)
        fingerprint = asset_fingerprint(ref.photo)
        if id_key in seen_ids or (fingerprint and fingerprint in seen_fingerprints):
            continue
        seen_ids.add(id_key)
        if fingerprint:
            seen_fingerprints.add(fingerprint)
        merged.append(ref)
    return merged


def _zone_label(zone: str) -> str:
    if zone == SHARED_ALBUMS_ZONE:
        return "shared album"
    if is_shared_library_zone(zone):
        return "shared library"
    return "personal library"
