"""Constants for the iCloud Shared Photos integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "icloud_shared_photos"

# Domain of the Home Assistant core iCloud integration whose authenticated
# PyiCloudService session is reused.
ICLOUD_DOMAIN: Final = "icloud"

MEDIA_SOURCE_TITLE: Final = "iCloud Shared Photos"

# Options
CONF_MAX_ITEMS: Final = "max_items"
CONF_INCLUDE_VIDEOS: Final = "include_videos"
CONF_IMAGE_VERSION: Final = "image_version"
CONF_CACHE_TTL: Final = "cache_ttl"

DEFAULT_MAX_ITEMS: Final = 500
DEFAULT_INCLUDE_VIDEOS: Final = False
DEFAULT_CACHE_TTL: Final = 300  # seconds; album listings kept in RAM

IMAGE_VERSION_AUTO: Final = "auto"
IMAGE_VERSION_ORIGINAL: Final = "original"
IMAGE_VERSION_MEDIUM: Final = "medium"
IMAGE_VERSIONS: Final = (
    IMAGE_VERSION_AUTO,
    IMAGE_VERSION_ORIGINAL,
    IMAGE_VERSION_MEDIUM,
)
DEFAULT_IMAGE_VERSION: Final = IMAGE_VERSION_AUTO

# How long a resolved PhotoAsset (and therefore its signed CloudKit download
# URLs) is trusted before it is looked up again.
ASSET_URL_TTL: Final = 1800  # seconds
MAX_ASSET_CACHE_SIZE: Final = 2000

# pyicloud / CloudKit naming
PRIMARY_ZONE_NAME: Final = "PrimarySync"
SHARED_LIBRARY_ZONE_PREFIX: Final = "SharedSync-"
ALBUM_LIBRARY: Final = "Library"
ALBUM_FAVORITES: Final = "Favorites"
SUPPORTED_ALBUMS: Final = (ALBUM_LIBRARY, ALBUM_FAVORITES)
# Pseudo zone for legacy Shared Albums (photo streams). Their "album" is the
# album GUID. Never a real CloudKit zone name.
SHARED_ALBUMS_ZONE: Final = "SharedAlbums"

# Favorites collections
FAVORITES_PERSONAL: Final = "personal"
FAVORITES_SHARED: Final = "shared"
FAVORITES_ALL: Final = "all"
FAVORITES_COLLECTIONS: Final = (FAVORITES_PERSONAL, FAVORITES_SHARED, FAVORITES_ALL)

# Services
SERVICE_GET_ALBUM_PHOTOS: Final = "get_album_photos"
ATTR_ALBUM: Final = "album"
ATTR_ACCOUNT: Final = "account"
ATTR_EXPIRES: Final = "expires"
DEFAULT_URL_EXPIRES: Final = 3600  # seconds

SERVE_URL: Final = "/api/icloud_shared_photos/serve/{version}/{token}"
SERVE_URL_PREFIX: Final = "/api/icloud_shared_photos/serve"
