"""Config flow for iCloud Shared Photos.

There is nothing to configure: credentials and 2FA are handled by the core
``icloud`` integration, whose session is reused. The flow only creates the
(single) config entry so the media source gets loaded, and offers a few
options.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_CACHE_TTL,
    CONF_IMAGE_VERSION,
    CONF_INCLUDE_VIDEOS,
    CONF_MAX_ITEMS,
    DEFAULT_CACHE_TTL,
    DEFAULT_IMAGE_VERSION,
    DEFAULT_INCLUDE_VIDEOS,
    DEFAULT_MAX_ITEMS,
    DOMAIN,
    ICLOUD_DOMAIN,
    IMAGE_VERSIONS,
    MEDIA_SOURCE_TITLE,
)


class IcloudSharedPhotosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iCloud Shared Photos."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup; requires the core iCloud integration."""
        if not self.hass.config_entries.async_entries(ICLOUD_DOMAIN):
            return self.async_abort(reason="icloud_not_configured")
        if user_input is not None:
            return self.async_create_entry(title=MEDIA_SOURCE_TITLE, data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return IcloudSharedPhotosOptionsFlow()


class IcloudSharedPhotosOptionsFlow(OptionsFlow):
    """Options for iCloud Shared Photos."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            user_input[CONF_MAX_ITEMS] = int(user_input[CONF_MAX_ITEMS])
            user_input[CONF_CACHE_TTL] = int(user_input[CONF_CACHE_TTL])
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MAX_ITEMS,
                    default=options.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10, max=10000, step=10, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_INCLUDE_VIDEOS,
                    default=options.get(CONF_INCLUDE_VIDEOS, DEFAULT_INCLUDE_VIDEOS),
                ): BooleanSelector(),
                vol.Required(
                    CONF_IMAGE_VERSION,
                    default=options.get(CONF_IMAGE_VERSION, DEFAULT_IMAGE_VERSION),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(IMAGE_VERSIONS),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_IMAGE_VERSION,
                    )
                ),
                vol.Required(
                    CONF_CACHE_TTL,
                    default=options.get(CONF_CACHE_TTL, DEFAULT_CACHE_TTL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=86400,
                        step=30,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
