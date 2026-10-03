"""Config and options flow for the Tesla / Autel BLE TPMS integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_CONNECT,
    CONF_PRESSURE_TRIM,
    CONF_PROFILE,
    CONF_TEMPERATURE_TRIM,
    DEFAULT_CONNECT,
    DEFAULT_PRESSURE_TRIM,
    DEFAULT_PROFILE,
    DEFAULT_TEMPERATURE_TRIM,
    DOMAIN,
)
from .decoder import PROFILES
from .parser import TeslaTPMSBluetoothDeviceData as DeviceData


class TeslaTPMSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tesla / Autel BLE TPMS."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: DeviceData | None = None
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a sensor discovered by the Bluetooth integration."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        device = DeviceData()
        if not device.supported(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery_info = discovery_info
        self._discovered_device = device
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a discovered sensor."""
        assert self._discovered_device is not None
        assert self._discovery_info is not None
        device = self._discovered_device
        title = (
            device.title or device.get_device_name() or self._discovery_info.name
        )
        if user_input is not None:
            return self.async_create_entry(title=title, data={})

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders=placeholders
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick from the sensors already seen."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_devices[address], data={}
            )

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            device = DeviceData()
            if device.supported(discovery_info):
                self._discovered_devices[address] = (
                    device.title or device.get_device_name() or discovery_info.name
                )

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices)}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return TeslaTPMSOptionsFlow()


class TeslaTPMSOptionsFlow(OptionsFlow):
    """Let the user pick a decoding profile and trim the readings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROFILE,
                        default=options.get(CONF_PROFILE, DEFAULT_PROFILE),
                    ): vol.In(sorted(PROFILES)),
                    vol.Optional(
                        CONF_PRESSURE_TRIM,
                        default=options.get(
                            CONF_PRESSURE_TRIM, DEFAULT_PRESSURE_TRIM
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_TEMPERATURE_TRIM,
                        default=options.get(
                            CONF_TEMPERATURE_TRIM, DEFAULT_TEMPERATURE_TRIM
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_CONNECT,
                        default=options.get(CONF_CONNECT, DEFAULT_CONNECT),
                    ): bool,
                }
            ),
        )
