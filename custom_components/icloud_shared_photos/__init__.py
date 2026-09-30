"""iCloud Shared Photos: expose the iCloud Shared Photo Library as a media source.

This integration does not log in to iCloud itself. It reuses the authenticated
``PyiCloudService`` of the Home Assistant core ``icloud`` integration, so there
is no second Apple ID configuration and no second 2FA prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .library import AccountPhotos
from .media_source import async_register_view

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class IcloudSharedPhotosData:
    """Runtime data: per iCloud account RAM caches, keyed by icloud entry_id."""

    accounts: dict[str, AccountPhotos] = field(default_factory=dict)


type IcloudSharedPhotosConfigEntry = ConfigEntry[IcloudSharedPhotosData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (registers the streaming HTTP view once)."""
    async_register_view(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: IcloudSharedPhotosConfigEntry
) -> bool:
    """Set up iCloud Shared Photos from a config entry."""
    entry.runtime_data = IcloudSharedPhotosData()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _LOGGER.debug("iCloud Shared Photos set up, options: %s", dict(entry.options))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: IcloudSharedPhotosConfigEntry
) -> bool:
    """Unload a config entry (drops all RAM caches)."""
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: IcloudSharedPhotosConfigEntry
) -> None:
    """Drop cached listings when options change."""
    for account in entry.runtime_data.accounts.values():
        account.invalidate()
